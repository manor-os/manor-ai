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
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from apps.api.file_responses import EntitySnapshotFileResponse, entity_filesystem_read_boundary
from apps.api.middleware.rate_limit import RateLimiter, client_ip
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
from packages.core.permissions import effective_user_has_permission, Permission
from packages.core.services.document_access import (
    document_is_owned_by_deleted_workspace,
    effective_document_folder_policy,
    effective_folder_policy,
    folder_is_owned_by_deleted_workspace,
    folder_grant_capabilities_for_user,
    folder_grants_for_user,
    lock_folder_policy_rows,
    lock_workspace_policy_rows,
    resolve_document_policy_lock_scope,
    resolve_folder_policy_lock_scope,
    user_can_read_folder_path,
    user_has_folder_capability,
)
from packages.core.services.resource_grant_policy import (
    ResourceGrantPolicyError,
    ResourceGrantPolicyFactory,
    revoke_resource_grant_family,
    upsert_manual_resource_grant,
)
from packages.core.services.entity_fs import get_entity_root, resolve_path
from packages.core.services.document_service import get_document_content
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

router = APIRouter(prefix="/api/v1/folders", tags=["folder-permissions"])

_SHARE_ACCESS_COOKIE = "manor_share_access"
_SHARE_OTP_LIMITER = RateLimiter()


# ── Helpers (shared with document_permissions; duplicated here to keep
# the import graph simple — they're tiny.) ───────────────────────────────


async def _load_folder(
    db: AsyncSession,
    folder_id: str,
    entity_id: str,
    *,
    for_update: bool = False,
) -> DocumentFolder:
    stmt = select(DocumentFolder).where(
        DocumentFolder.id == folder_id,
        DocumentFolder.entity_id == entity_id,
    )
    if not for_update:
        folder = (await db.execute(stmt)).scalar_one_or_none()
        if not folder or await folder_is_owned_by_deleted_workspace(db, folder):
            raise CodedError(
                404,
                code="permissions.error.folder.not_found",
                message="Folder not found",
            )
        return folder

    for _attempt in range(3):
        savepoint = await db.begin_nested()
        try:
            folder = (await db.execute(stmt)).scalar_one_or_none()
            if not folder:
                raise CodedError(
                    404,
                    code="permissions.error.folder.not_found",
                    message="Folder not found",
                )
            scope = await resolve_folder_policy_lock_scope(
                db,
                entity_id=entity_id,
                folder_id=folder.id,
            )
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=entity_id,
                workspace_ids=scope.workspace_ids,
                read=True,
            )
            if any(workspace.deleted_at is not None for workspace in workspaces):
                raise CodedError(
                    404,
                    code="permissions.error.folder.not_found",
                    message="Folder not found",
                )
            await lock_folder_policy_rows(
                db,
                entity_id=entity_id,
                folder_id=folder.id,
                folder_ids=tuple(
                    ancestor_id
                    for ancestor_id in scope.folder_ids
                    if ancestor_id != folder.id
                ),
                read=True,
            )
            folder = (await db.execute(
                stmt.with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if not folder:
                raise CodedError(
                    404,
                    code="permissions.error.folder.not_found",
                    message="Folder not found",
                )
            if await resolve_folder_policy_lock_scope(
                db,
                entity_id=entity_id,
                folder_id=folder.id,
            ) != scope:
                await savepoint.rollback()
                continue
            await savepoint.commit()
            return folder
        except Exception:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise CodedError(
        409,
        code="permissions.error.folder.changed_during_request",
        message="Folder changed during the request; please retry",
    )


async def _is_owner_or_admin(
    db: AsyncSession,
    folder: DocumentFolder,
    user: User,
) -> bool:
    if folder.owner_id and folder.owner_id == user.id:
        return True
    return await effective_user_has_permission(db, user, Permission.ADMIN_SETTINGS)


async def _can_create_internal_grant(
    db: AsyncSession,
    folder: DocumentFolder,
    user: User,
) -> bool:
    if await _is_owner_or_admin(db, folder, user):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS, Capability.SHARE_INTERNAL},
    )


async def _can_administer_internal_acl(
    db: AsyncSession,
    folder: DocumentFolder,
    user: User,
) -> bool:
    if await _is_owner_or_admin(db, folder, user):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.GRANT_ACCESS},
    )


async def _can_manage_external_share(db: AsyncSession, folder: DocumentFolder, user: User) -> bool:
    if await _is_owner_or_admin(db, folder, user):
        return True
    return await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.SHARE_EXTERNAL},
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
    # When true (default), persist the new floors/ceilings on every existing
    # descendant. With false, folder visibility is enforced lazily on reads,
    # but unsafe classification/client-visibility drift fails atomically.
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
    folder = await _load_folder(db, folder_id, user.entity_id, for_update=True)
    if not await _is_owner_or_admin(db, folder, user):
        raise HTTPException(
            403, "Only the folder owner or an admin can change folder properties",
        )

    final_visibility = req.visibility or folder.visibility or "entity"
    final_classification = req.classification or folder.classification or Classification.INTERNAL
    final_client_visible = (
        req.client_visible
        if req.client_visible is not None
        else bool(folder.client_visible)
    )

    # Validate the final state, not only fields present in this PATCH-like
    # request. Otherwise two individually-valid updates can create an invalid
    # restricted/public or confidential/client-visible combination.
    if final_classification == Classification.RESTRICTED and final_visibility == "public":
        raise HTTPException(
            400, "Restricted folder cannot have public visibility",
        )
    if final_client_visible and final_classification in (
        Classification.CONFIDENTIAL, Classification.RESTRICTED,
    ):
        final_client_visible = False

    # Moving/updating a child may never undercut an ancestor's classification
    # floor or exceed its visibility ceiling.
    all_folders = list((await db.execute(
        select(DocumentFolder).where(DocumentFolder.entity_id == user.entity_id)
    )).scalars().all())
    folder_by_id = {item.id: item for item in all_folders}
    current_parent_id = folder.parent_id
    seen: set[str] = set()
    while current_parent_id and current_parent_id not in seen:
        seen.add(current_parent_id)
        parent = folder_by_id.get(current_parent_id)
        if parent is None:
            break
        parent_classification = parent.classification or Classification.INTERNAL
        parent_visibility = parent.visibility or "entity"
        if _CLASS_RANK[final_classification] < _CLASS_RANK[parent_classification]:
            raise HTTPException(400, "Folder classification cannot be below its parent")
        if _VIS_RANK[final_visibility] > _VIS_RANK[parent_visibility]:
            raise HTTPException(400, "Folder visibility cannot be broader than its parent")
        current_parent_id = parent.parent_id

    # Apply to the folder row first.
    folder.visibility = final_visibility
    folder.classification = final_classification
    folder.client_visible = final_client_visible

    cascade_summary: dict = {"docs_updated": 0, "subfolders_updated": 0}

    if req.classification is not None or req.visibility is not None or req.client_visible is not None:
        # Walk all descendant folders + child docs. Folder policy is an
        # immediate invariant: cascade=false may leave visibility narrowing to
        # the canonical read path, but cannot leave lower-classification or
        # client-visible content beneath a protected folder.
        descendant_folders = await _walk_descendant_folders(
            db, folder.id, user.entity_id,
        )
        folder_ids = [folder.id] + [f.id for f in descendant_folders]
        docs = (
            await db.execute(
                select(Document).where(
                    Document.entity_id == user.entity_id,
                    Document.folder_id.in_(folder_ids),
                )
            )
        ).scalars().all()

        def _needs_policy_update(item) -> bool:
            return (
                _CLASS_RANK.get(item.classification or Classification.INTERNAL, 1)
                < _CLASS_RANK[final_classification]
                or (
                    not final_client_visible
                    and bool(getattr(item, "client_visible", False))
                )
            )

        if not req.cascade and any(
            _needs_policy_update(item) for item in [*descendant_folders, *docs]
        ):
            raise HTTPException(
                409,
                "Existing folder contents violate the new policy; enable cascade",
            )

        if not req.cascade:
            descendant_folders = []
            docs = []

        for sub in descendant_folders:
            if _CLASS_RANK.get(sub.classification or Classification.INTERNAL, 1) < _CLASS_RANK[final_classification]:
                sub.classification = final_classification
                cascade_summary["subfolders_updated"] += 1
            if _VIS_RANK.get(sub.visibility or "entity", 2) > _VIS_RANK[final_visibility]:
                sub.visibility = final_visibility
                cascade_summary["subfolders_updated"] += 1
            if not final_client_visible:
                sub.client_visible = False

        # All docs whose folder_id is folder.id or any descendant.
        for doc in docs:
            if _CLASS_RANK.get(doc.classification or Classification.INTERNAL, 1) < _CLASS_RANK[final_classification]:
                doc.classification = final_classification
                cascade_summary["docs_updated"] += 1
            if _VIS_RANK.get(doc.visibility or "entity", 2) > _VIS_RANK[final_visibility]:
                doc.visibility = final_visibility
                cascade_summary["docs_updated"] += 1
            if not final_client_visible:
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
    if not await _can_administer_internal_acl(db, folder, user):
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
    folder = await _load_folder(
        db,
        folder_id,
        user.entity_id,
        for_update=True,
    )
    owner_or_admin = await _is_owner_or_admin(db, folder, user)
    if not owner_or_admin and not await _can_create_internal_grant(
        db,
        folder,
        user,
    ):
        raise HTTPException(
            403,
            "Only the folder owner/admin or a user with internal share access can grant access",
        )
    if req.subject_type != SubjectType.USER:
        raise HTTPException(400, "Folder grants currently support only user subjects")

    grantor_capabilities = None
    if not owner_or_admin:
        grantor_capabilities = await folder_grant_capabilities_for_user(
            db,
            entity_id=user.entity_id,
            folder_id=folder.id,
            user_id=user.id,
        )
    expires_at = req.expires_at
    try:
        policy = ResourceGrantPolicyFactory.create(
            ResourceType.DOCUMENT_FOLDER
        )
        capabilities = policy.validate(
            req.capabilities,
            grantor_capabilities=grantor_capabilities,
        )
        if not owner_or_admin:
            expires_at = policy.delegated_expiry(
                capabilities,
                grants=await folder_grants_for_user(
                    db, entity_id=user.entity_id, folder_id=folder.id, user_id=user.id,
                ),
                expires_at=expires_at,
            )
    except ResourceGrantPolicyError as exc:
        raise HTTPException(
            403 if grantor_capabilities is not None else 400,
            str(exc),
        ) from exc

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
        resource_type=ResourceType.DOCUMENT_FOLDER,
        resource_id=folder.id,
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


@router.delete("/{folder_id}/grants/{grant_id}", status_code=204)
async def revoke_folder_grant(
    folder_id: str,
    grant_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await _load_folder(
        db,
        folder_id,
        user.entity_id,
        for_update=True,
    )
    if not await _can_administer_internal_acl(db, folder, user):
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
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if not grant:
        raise CodedError(
            404,
            code="permissions.error.folder.grant_not_found",
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
    expires_in_days: int | None = Field(default=7, ge=1, le=90)
    watermark: bool = True
    require_otp: bool = False
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
    folder = await _load_folder(db, folder_id, user.entity_id, for_update=True)
    if not await _can_manage_external_share(db, folder, user):
        raise CodedError(
            403,
            code="permissions.error.folder.share_external_owner_only",
            message="Only the folder owner/admin or a user with external share access can share externally",
        )

    cls, _effective_visibility, _effective_client_visible = await effective_folder_policy(
        db, folder
    )
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
    if Capability.VIEW not in req.capabilities:
        raise HTTPException(400, "External shares must include the view capability")

    if req.audience_type == "anonymous":
        audience = "anonymous"
    elif req.audience_type == "email":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for email audience")
        if not req.require_otp:
            raise HTTPException(400, "Email audience shares require OTP verification")
        audience = f"email:{req.audience_value.strip().lower()}"
    elif req.audience_type == "domain":
        if not req.audience_value:
            raise HTTPException(400, "audience_value required for domain audience")
        if not req.require_otp:
            raise HTTPException(400, "Domain audience shares require OTP verification")
        audience = f"domain:{req.audience_value.strip().lower().lstrip('@')}"
    else:
        raise HTTPException(400, f"Invalid audience_type: {req.audience_type}")

    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=req.expires_in_days) if req.expires_in_days is not None else None
    if not await _is_owner_or_admin(db, folder, user):
        policy = ResourceGrantPolicyFactory.create(ResourceType.DOCUMENT_FOLDER)
        grants = await folder_grants_for_user(
            db, entity_id=user.entity_id, folder_id=folder.id, user_id=user.id,
        )
        implicit_view = await user_can_read_folder_path(
            db, folder_id=folder.id, entity_id=user.entity_id, user_id=user.id, role=user.role,
            allow_redacted=False, include_grants=False,
        )
        try:
            expires_at = policy.delegated_expiry(
                req.capabilities,
                grants=grants,
                expires_at=expires_at,
                authority_capabilities=(Capability.SHARE_EXTERNAL,),
                clamp_expiry="expires_in_days" not in req.model_fields_set,
                implicit_capabilities=(Capability.VIEW,) if implicit_view else (),
            )
        except ResourceGrantPolicyError as exc:
            raise HTTPException(403, str(exc)) from exc
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
        created_at=now,
        expires_at=expires_at,
        max_uses=None,
        use_count=0,
        status="active",
        metadata_={
            "classification_at_creation": cls,
        },
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
    folder = await _load_folder(db, folder_id, user.entity_id, for_update=True)
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
    mime_type: str | None = None
    classification: str | None = None


class SharedFolderResponse(BaseModel):
    folder_id: str
    name: str
    classification: str | None = None
    capabilities: list[str]
    watermark: bool
    allow_download: bool
    expires_at: datetime | None = None
    parent_id: str | None = None
    # Direct (non-recursive) children of the shared folder. Subfolder
    # contents are not enumerated to keep the public surface small;
    # subfolders themselves appear in the listing so the user knows the
    # structure exists, but their contents require navigation through
    # additional shares.
    documents: list[PublicFolderDocResponse] = Field(default_factory=list)
    subfolders: list[dict] = Field(default_factory=list)  # [{id, name}]


public_router = APIRouter(prefix="/api/v1/shared-folder", tags=["public-share"])


async def _load_public_folder_share(
    db: AsyncSession,
    token: str,
    *,
    share_for_update: bool = False,
    lock_direct_children: bool = False,
    folder_id: str | None = None,
    document_id: str | None = None,
    allow_use_limit_reached: bool = False,
) -> tuple[
    Share,
    DocumentFolder,
    datetime,
    list[Document],
    list[DocumentFolder],
]:
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
            if share_scope.resource_type != ResourceType.DOCUMENT_FOLDER:
                raise HTTPException(404, "Not a folder share")

            target_folder_id = folder_id or share_scope.resource_id
            if document_id is not None:
                candidate = (await db.execute(select(Document).where(
                    Document.id == document_id,
                    Document.entity_id == share_scope.entity_id,
                    Document.is_trashed == False,  # noqa: E712
                ))).scalar_one_or_none()
                if not candidate or not candidate.folder_id:
                    raise HTTPException(404, "Shared document not found")
                target_folder_id = candidate.folder_id
            folder_stmt = select(DocumentFolder).where(
                DocumentFolder.id == target_folder_id,
                DocumentFolder.entity_id == share_scope.entity_id,
            )
            folder = (await db.execute(folder_stmt)).scalar_one_or_none()
            if not folder:
                raise HTTPException(404, "Underlying folder not found")
            scope = await resolve_folder_policy_lock_scope(
                db,
                entity_id=folder.entity_id,
                folder_id=folder.id,
            )
            if share_scope.resource_id not in scope.folder_ids:
                raise HTTPException(404, "Folder is outside this share")
            direct_documents: list[Document] = []
            direct_folders: list[DocumentFolder] = []
            document_scopes = {}
            child_folder_scopes = {}
            workspace_ids = set(scope.workspace_ids)
            folder_ids = set(scope.folder_ids)
            if lock_direct_children:
                direct_documents = list((await db.execute(
                    select(Document)
                    .where(
                        Document.entity_id == folder.entity_id,
                        Document.folder_id == folder.id,
                        Document.is_trashed == False,  # noqa: E712
                    )
                    .order_by(Document.id)
                )).scalars())
                direct_folders = list((await db.execute(
                    select(DocumentFolder)
                    .where(
                        DocumentFolder.entity_id == folder.entity_id,
                        DocumentFolder.parent_id == folder.id,
                    )
                    .order_by(DocumentFolder.id)
                )).scalars())
                for document in direct_documents:
                    document_scope = await resolve_document_policy_lock_scope(
                        db,
                        document,
                    )
                    document_scopes[document.id] = document_scope
                    workspace_ids.update(document_scope.workspace_ids)
                    folder_ids.update(document_scope.folder_ids)
                for child in direct_folders:
                    child_scope = await resolve_folder_policy_lock_scope(
                        db,
                        entity_id=child.entity_id,
                        folder_id=child.id,
                    )
                    child_folder_scopes[child.id] = child_scope
                    workspace_ids.update(child_scope.workspace_ids)
                    folder_ids.update(child_scope.folder_ids)
            workspaces = await lock_workspace_policy_rows(
                db,
                entity_id=folder.entity_id,
                workspace_ids=tuple(sorted(workspace_ids)),
                read=True,
            )
            if any(
                workspace.id in scope.workspace_ids
                and workspace.deleted_at is not None
                for workspace in workspaces
            ):
                raise CodedError(
                    410,
                    code="permissions.error.share.revoked_by_workspace_lifecycle",
                    message="Share is no longer available",
                )
            await lock_folder_policy_rows(
                db,
                entity_id=folder.entity_id,
                folder_id=folder.id,
                folder_ids=tuple(sorted(folder_ids)),
                read=True,
            )
            folder = (await db.execute(
                folder_stmt.with_for_update(read=True).execution_options(
                    populate_existing=True
                )
            )).scalar_one_or_none()
            if not folder:
                raise HTTPException(404, "Underlying folder not found")
            if await resolve_folder_policy_lock_scope(
                db,
                entity_id=folder.entity_id,
                folder_id=folder.id,
            ) != scope:
                await savepoint.rollback()
                continue

            if lock_direct_children:
                locked_folders = list((await db.execute(
                    select(DocumentFolder)
                    .where(
                        DocumentFolder.entity_id == folder.entity_id,
                        DocumentFolder.parent_id == folder.id,
                    )
                    .order_by(DocumentFolder.id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )).scalars())
                locked_documents = list((await db.execute(
                    select(Document)
                    .where(
                        Document.entity_id == folder.entity_id,
                        Document.folder_id == folder.id,
                        Document.is_trashed == False,  # noqa: E712
                    )
                    .order_by(Document.id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )).scalars())
                if (
                    [child.id for child in locked_folders]
                    != [child.id for child in direct_folders]
                    or [document.id for document in locked_documents]
                    != [document.id for document in direct_documents]
                ):
                    await savepoint.rollback()
                    continue
                scopes_changed = False
                for child in locked_folders:
                    if await resolve_folder_policy_lock_scope(
                        db,
                        entity_id=child.entity_id,
                        folder_id=child.id,
                    ) != child_folder_scopes[child.id]:
                        scopes_changed = True
                        break
                if not scopes_changed:
                    for document in locked_documents:
                        if await resolve_document_policy_lock_scope(
                            db,
                            document,
                        ) != document_scopes[document.id]:
                            scopes_changed = True
                            break
                if scopes_changed:
                    await savepoint.rollback()
                    continue
                direct_folders = locked_folders
                direct_documents = locked_documents

            share = (await db.execute(
                select(Share)
                .where(
                    Share.id == share_scope.id,
                    Share.token_hash == token_hash,
                    Share.status == "active",
                    Share.resource_type == ResourceType.DOCUMENT_FOLDER,
                    Share.resource_id == share_scope.resource_id,
                    Share.entity_id == folder.entity_id,
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
            if share.max_uses is not None and (share.use_count or 0) >= share.max_uses and not allow_use_limit_reached:
                raise CodedError(
                    410,
                    code="permissions.error.share.use_limit_reached",
                    message="Share link reached use limit",
                )
            if Capability.VIEW not in set(share.capabilities or []):
                raise CodedError(403, code="permissions.error.share.view_not_allowed", message="Share does not allow viewing")
            current_classification, _effective_visibility, _effective_client_visible = (
                await effective_folder_policy(db, folder)
            )
            if current_classification in {Classification.CONFIDENTIAL, Classification.RESTRICTED}:
                raise CodedError(410, code="permissions.error.share.revoked_by_policy", message="Share is no longer available")
            root = await db.get(DocumentFolder, share.resource_id)
            if root is None:
                raise HTTPException(404, "Underlying folder not found")
            root_classification, _, _ = await effective_folder_policy(db, root)
            created_classification = dict(getattr(share, "metadata_", {}) or {}).get(
                "classification_at_creation"
            )
            if created_classification in _CLASS_RANK and (
                _CLASS_RANK[root_classification] > _CLASS_RANK[created_classification]
            ):
                raise CodedError(410, code="permissions.error.share.revoked_by_policy", message="Share is no longer available")
            await savepoint.commit()
            return share, folder, now, direct_documents, direct_folders
        except Exception:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    raise CodedError(
        409,
        code="permissions.error.share.resource_changed",
        message="Shared folder changed during the request; please retry",
    )


def _require_public_folder_access(share: Share, access_token: str | None) -> str | None:
    try:
        return verify_share_access(share, access_token)
    except ShareAccessError as exc:
        raise CodedError(
            401,
            code="permissions.error.share.verification_required",
            message=str(exc),
        ) from exc


def _require_folder_view_session(share: Share, request: Request) -> None:
    if share.max_uses is not None and not verify_share_view_session(
        share, request.cookies.get("manor_folder_share_view"),
    ):
        raise CodedError(
            410, code="permissions.error.share.use_limit_reached",
            message="Open the shared folder before accessing its contents",
        )


async def _load_shared_folder_document(
    db: AsyncSession, token: str, document_id: str, request: Request, *, download: bool,
) -> tuple[Share, Document]:
    share, _folder, _now, documents, _folders = await _load_public_folder_share(
        db, token, document_id=document_id, lock_direct_children=True,
        share_for_update=download, allow_use_limit_reached=True,
    )
    _require_public_folder_access(share, request.cookies.get(_SHARE_ACCESS_COOKIE))
    _require_folder_view_session(share, request)
    if download and (
        Capability.DOWNLOAD not in set(share.capabilities or []) or not share.allow_download
    ):
        raise CodedError(
            403, code="permissions.error.share.download_not_allowed",
            message="This share link does not allow downloading the file",
        )
    document = next((doc for doc in documents if doc.id == document_id), None)
    if (
        document is None
        or document.quarantine_status in {"quarantined", "rejected"}
        or await document_is_owned_by_deleted_workspace(db, document)
    ):
        raise HTTPException(404, "Shared document not found")
    classification, _, _ = await effective_document_folder_policy(db, document)
    if classification not in {Classification.PUBLIC, Classification.INTERNAL}:
        raise HTTPException(404, "Shared document not found")
    return share, document


async def _shared_folder_file_response(
    token: str, document_id: str, request: Request, db: AsyncSession, *, download: bool,
):
    share, document = await _load_shared_folder_document(
        db, token, document_id, request, download=download,
    )
    read_boundary = None
    try:
        if document.fs_path:
            entity_id = str(document.entity_id)
            await db.rollback()
            read_boundary = entity_filesystem_read_boundary(get_entity_root(entity_id))
            await read_boundary.__aenter__()
            share, document = await _load_shared_folder_document(
                db, token, document_id, request, download=download,
            )
            if str(document.entity_id) != entity_id:
                raise HTTPException(409, "Shared document changed; retry")
        headers = {
            "Cache-Control": "private, no-store",
            "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{urllib.parse.quote(document.name)}",
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        }
        media_type = document.mime_type or "application/octet-stream"
        if document.fs_path:
            path = resolve_path(document.entity_id, document.fs_path)
            if not path or not os.path.isfile(path):
                raise HTTPException(404, "Shared file is unavailable")
            result = EntitySnapshotFileResponse(
                path=path, media_type=media_type, headers=headers, read_boundary=read_boundary,
            )
        else:
            from apps.api.routers.documents import _metadata_document_file_response

            content = await get_document_content(db, document.id, document.entity_id)
            if content is None:
                raise HTTPException(404, "Shared file is unavailable")
            result = await _metadata_document_file_response(
                name=document.name, file_type=document.file_type,
                mime_type=document.mime_type, content=content,
            )
            disposition = result.headers["Content-Disposition"]
            headers["Content-Disposition"] = (
                disposition if download else disposition.replace("attachment;", "inline;", 1)
            )
            result.headers.update(headers)
        if download and share.max_uses is None:
            share.use_count = (share.use_count or 0) + 1
            share.last_used_at = datetime.now(timezone.utc)
        await db.commit()
        # Only a physical response owns the boundary through snapshot creation.
        if read_boundary is not None and not isinstance(result, EntitySnapshotFileResponse):
            await read_boundary.__aexit__(None, None, None)
        return result
    except BaseException:
        if read_boundary is not None:
            await read_boundary.__aexit__(None, None, None)
        raise


@public_router.get("/{token}/documents/{document_id}/content")
async def view_shared_folder_document(
    token: str, document_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    return await _shared_folder_file_response(token, document_id, request, db, download=False)


@public_router.get("/{token}/documents/{document_id}/download")
async def download_shared_folder_document(
    token: str, document_id: str, request: Request, db: AsyncSession = Depends(get_db),
):
    return await _shared_folder_file_response(token, document_id, request, db, download=True)


class ShareOtpRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ShareOtpVerifyRequest(ShareOtpRequest):
    code: str = Field(min_length=6, max_length=6)


async def _limit_share_otp(request: Request, share_id: str, purpose: str) -> None:
    max_requests = 5 if purpose == "request" else 10
    window_seconds = 10 * 60
    result = await _SHARE_OTP_LIMITER.check(
        f"folder-share-otp:{purpose}:{share_id}:{client_ip(request)}",
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
async def request_shared_folder_otp(
    token: str,
    req: ShareOtpRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    share, _folder, _now, _documents, _folders = (
        await _load_public_folder_share(db, token)
    )
    share_id = str(share.id)
    await db.rollback()
    await _limit_share_otp(request, share_id, "request")
    share, _folder, _now, _documents, _folders = await _load_public_folder_share(
        db,
        token,
        share_for_update=True,
    )
    if not share_requires_verification(share):
        raise HTTPException(400, "This share does not require email verification")
    try:
        challenge = create_otp_challenge_record(share, req.email)
    except ShareAccessError:
        await db.rollback()
        return {"status": "sent"}
    from packages.core.services.email_service import send_share_verification_email

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
async def verify_shared_folder_otp(
    token: str,
    req: ShareOtpVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    share, _folder, _now, _documents, _folders = (
        await _load_public_folder_share(db, token)
    )
    share_id = str(share.id)
    await db.rollback()
    await _limit_share_otp(request, share_id, "verify")
    share, _folder, _now, _documents, _folders = await _load_public_folder_share(
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
        path=f"/api/v1/shared-folder/{token}",
    )
    await db.commit()
    return {"access_token": access_token, "expires_in": 3600}


@public_router.get("/{token}", response_model=SharedFolderResponse)
async def view_shared_folder(
    token: str,
    request: Request,
    response: Response,
    access_token: str | None = Query(None),
    folder_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Unauthenticated viewer for a folder shared via opaque token.

    Returns the folder metadata + a flat list of its direct child documents
    (not recursive). Subfolder list is included so the recipient sees the
    structure exists even though contents aren't enumerated.
    """
    presented_access_token = access_token or request.cookies.get(
        _SHARE_ACCESS_COOKIE
    )
    share, _folder, _now, _documents, _folders = (
        await _load_public_folder_share(db, token, folder_id=folder_id, allow_use_limit_reached=folder_id is not None)
    )
    _require_public_folder_access(share, presented_access_token)
    if folder_id is not None and share.max_uses is not None:
        _require_folder_view_session(share, request)
    await db.rollback()

    share, folder, now, docs, subfolders = await _load_public_folder_share(
        db,
        token,
        share_for_update=True,
        lock_direct_children=True,
        folder_id=folder_id,
        allow_use_limit_reached=folder_id is not None,
    )
    current_classification, _effective_visibility, _effective_client_visible = (
        await effective_folder_policy(db, folder)
    )
    _verified_email = _require_public_folder_access(share, presented_access_token)
    if folder_id is not None and share.max_uses is not None:
        _require_folder_view_session(share, request)
    else:
        share.use_count = (share.use_count or 0) + 1
        share.last_used_at = now
    if share.max_uses is not None:
        response.set_cookie(
            "manor_folder_share_view", create_share_view_session(share=share, use_count=share.use_count),
            max_age=3600, httponly=True, secure=request.url.scheme == "https",
            samesite="strict", path=f"/api/v1/shared-folder/{token}",
        )

    # A folder share must not silently expand into confidential/restricted or
    # quarantined children. Those require their own explicit policy path.
    visible_docs: list[tuple[Document, str]] = []
    for doc in docs:
        if await document_is_owned_by_deleted_workspace(db, doc):
            continue
        effective_classification, _visibility, _client_visible = (
            await effective_document_folder_policy(db, doc)
        )
        if (
            getattr(doc, "quarantine_status", None) not in {"quarantined", "rejected"}
            and _CLASS_RANK.get(effective_classification, 1)
            <= _CLASS_RANK[Classification.INTERNAL]
        ):
            visible_docs.append((doc, effective_classification))
    visible_subfolders = []
    for child in subfolders:
        if await folder_is_owned_by_deleted_workspace(db, child):
            continue
        effective_classification, _visibility, _client_visible = await effective_folder_policy(
            db, child
        )
        if (
            _CLASS_RANK.get(effective_classification, 1)
            <= _CLASS_RANK[Classification.INTERNAL]
        ):
            visible_subfolders.append(child)
    subfolders = visible_subfolders

    await db.commit()
    _ = request  # not used yet; left for future audit-log wiring per-doc preview
    return SharedFolderResponse(
        folder_id=folder.id,
        name=folder.name,
        classification=current_classification,
        capabilities=list(share.capabilities or []),
        watermark=bool(share.watermark),
        allow_download=bool(share.allow_download),
        expires_at=share.expires_at,
        parent_id=folder.parent_id if folder.id != share.resource_id else None,
        documents=[
            PublicFolderDocResponse(
                id=doc.id,
                name=doc.name,
                file_size=doc.file_size,
                file_type=doc.file_type,
                mime_type=doc.mime_type,
                classification=effective_classification,
            )
            for doc, effective_classification in visible_docs
        ],
        subfolders=[{"id": f.id, "name": f.name} for f in subfolders],
    )
