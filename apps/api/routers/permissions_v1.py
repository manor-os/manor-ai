"""Permission-v1 endpoints — knowledge-base classification, legal hold,
visibility changes, and access requests.

Authorization uses the same effective-capability model as the rest of the
document surface (``packages.core.services.document_access``): the document
owner and entity owner/admin hold every capability; other users need an
explicit ``resource_grants`` row (direct or inherited from a folder).

Sister-RFC: ``docs/PERMISSIONS_DESIGN_ZH.md`` §13.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from packages.core.database import get_db
from packages.core.models import (
    Capability,
    Classification,
    Visibility,
)
from packages.core.models.document import Document
from packages.core.models.permission import (
    PendingStatus,
    ResourceGrantPending,
)
from packages.core.models.user import User
from packages.core.permissions import (
    Permission,
    has_permission,
    user_has_permission,
)
from packages.core.services.document_access import (
    effective_document_capabilities_for_user,
)

router = APIRouter(prefix="/api/v1/permissions", tags=["permissions-v1"])


# ── Request models ───────────────────────────────────────────────────────


class ClassifyRequest(BaseModel):
    classification: str = Field(
        ..., description="One of: public | internal | confidential | restricted"
    )
    note: Optional[str] = None


class VisibilityRequest(BaseModel):
    visibility: str = Field(
        ..., description="One of: private | workspace | entity | public"
    )


class ClientVisibleRequest(BaseModel):
    """Toggle whether a document appears in the client portal.

    Enforces invariant 4 (RFC §13.14): documents at Confidential or
    Restricted classification cannot be client-visible.
    """
    client_visible: bool


class AccessRequestPayload(BaseModel):
    resource_type: str
    resource_id: str
    requested_capabilities: list[str] = Field(default_factory=lambda: [Capability.VIEW])
    reason: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────


_VALID_CLASSIFICATIONS = set(Classification.LEVELS)
_VALID_VISIBILITIES = {
    Visibility.PRIVATE,
    Visibility.WORKSPACE,
    Visibility.ENTITY,
    Visibility.PUBLIC,
}


async def _load_doc(db: AsyncSession, document_id: str, entity_id: str) -> Document:
    doc = (
        await db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise CodedError(
            404,
            code="permissions.error.doc.not_found",
            message="Document not found",
        )
    return doc


async def _require_document_capability(
    db: AsyncSession,
    user: User,
    doc: Document,
    capability: str,
    message: str,
) -> None:
    """403 unless the user is the doc owner, an entity owner/admin, or
    holds an explicit grant carrying ``capability`` (direct or via a
    folder ancestor). Same model as the documents router."""
    capabilities = await effective_document_capabilities_for_user(
        db,
        document=doc,
        user_id=user.id,
        role=user.role,
    )
    if capability not in capabilities:
        raise HTTPException(403, message)


async def _is_audit_admin(db: AsyncSession, user: User) -> bool:
    if has_permission(user.role, Permission.ADMIN_AUDIT):
        return True
    return await user_has_permission(
        db, user.id, user.entity_id, Permission.ADMIN_AUDIT
    )


async def _audit(
    db: AsyncSession,
    doc: Document,
    user: User,
    action: str,
    request: Request | None,
) -> None:
    from apps.api.routers.document_permissions import _write_access_log

    await _write_access_log(
        db,
        doc=doc,
        actor_type="user",
        actor_id=user.id,
        action=action,
        request=request,
    )


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/documents/{document_id}/classify")
async def reclassify_document(
    document_id: str,
    body: ClassifyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a document's classification.

    Authorization: doc owner / entity admin, or a ``reclassify`` grant.
    Classification *downgrades* additionally require the ``admin.audit``
    verb — dropping confidentiality must not happen silently (RFC §13.3).
    """
    if body.classification not in _VALID_CLASSIFICATIONS:
        raise HTTPException(
            400,
            f"Invalid classification (allowed: {sorted(_VALID_CLASSIFICATIONS)})",
        )
    doc = await _load_doc(db, document_id, user.entity_id)
    await _require_document_capability(
        db,
        user,
        doc,
        Capability.RECLASSIFY,
        "Only the document owner/admin or a user with reclassify access "
        "can change classification",
    )

    # Invariant 1 (RFC §13.14): restricted ⇒ visibility ≠ public.
    if (
        body.classification == Classification.RESTRICTED
        and getattr(doc, "visibility", None) == Visibility.PUBLIC
    ):
        raise HTTPException(
            409, "restricted documents cannot have public visibility"
        )

    # Refuse silent classification *downgrade* — requires the admin.audit
    # verb to drop confidentiality. (Invariant 13.3.)
    current = getattr(doc, "classification", Classification.INTERNAL)
    if Classification.rank(body.classification) < Classification.rank(current):
        if not await _is_audit_admin(db, user):
            raise HTTPException(
                403,
                "Classification downgrade requires admin audit permission",
            )

    doc.classification = body.classification
    await _audit(db, doc, user, "reclassify", request)
    await db.commit()
    await db.refresh(doc)
    return {
        "id": doc.id,
        "classification": doc.classification,
        "previous": current,
    }


@router.post("/documents/{document_id}/visibility")
async def change_document_visibility(
    document_id: str,
    body: VisibilityRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.visibility not in _VALID_VISIBILITIES:
        raise HTTPException(
            400,
            f"Invalid visibility (allowed: {sorted(_VALID_VISIBILITIES)})",
        )
    doc = await _load_doc(db, document_id, user.entity_id)
    await _require_document_capability(
        db,
        user,
        doc,
        Capability.MANAGE_METADATA,
        "Only the document owner/admin or a user with metadata access "
        "can change visibility",
    )

    # Invariant 1: restricted ⇒ visibility ≠ public
    if (
        getattr(doc, "classification", None) == Classification.RESTRICTED
        and body.visibility == Visibility.PUBLIC
    ):
        raise HTTPException(
            409, "restricted documents cannot have public visibility"
        )

    doc.visibility = body.visibility
    await _audit(db, doc, user, "visibility_change", request)
    await db.commit()
    return {"id": doc.id, "visibility": doc.visibility}


@router.post("/documents/{document_id}/client-visible")
async def set_document_client_visible(
    document_id: str,
    body: ClientVisibleRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle ``client_visible`` on a document.

    Same authorization as visibility/classification (``manage_metadata``).
    Refuses if the document is Confidential or Restricted — those
    classifications imply the client portal must not surface the doc
    (invariant 4, RFC §13.14). The frontend disables the toggle in those
    cases, but the server gate is the source of truth.
    """
    doc = await _load_doc(db, document_id, user.entity_id)
    await _require_document_capability(
        db,
        user,
        doc,
        Capability.MANAGE_METADATA,
        "Only the document owner/admin or a user with metadata access "
        "can change client visibility",
    )

    cls = getattr(doc, "classification", None)
    if body.client_visible and cls in (Classification.CONFIDENTIAL, Classification.RESTRICTED):
        raise CodedError(
            409,
            code="permissions.error.doc.client_visible_blocked_by_classification",
            message="Confidential/Restricted documents cannot be client-visible",
        )

    doc.client_visible = body.client_visible
    await _audit(db, doc, user, "client_visible_change", request)
    await db.commit()
    return {"id": doc.id, "client_visible": doc.client_visible}


@router.post("/access-requests")
async def request_access(
    body: AccessRequestPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """User-facing 'request access' flow (RFC §13.10).

    Anyone authenticated can submit; the resource owner / curator approves
    via a separate admin endpoint. We deliberately do not check that the
    user *currently* lacks access — overlapping requests are harmless and
    audit-friendly.
    """
    pending = ResourceGrantPending(
        entity_id=user.entity_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        requester_user_id=user.id,
        requested_capabilities=body.requested_capabilities,
        reason=body.reason,
        status=PendingStatus.PENDING,
    )
    db.add(pending)
    await db.commit()
    await db.refresh(pending)
    return {
        "id": pending.id,
        "status": pending.status,
        "resource_type": pending.resource_type,
        "resource_id": pending.resource_id,
    }


@router.get("/access-requests")
async def list_access_requests(
    status: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List pending requests scoped to the caller's entity."""
    is_admin = await _is_audit_admin(db, user)

    q = select(ResourceGrantPending).where(
        ResourceGrantPending.entity_id == user.entity_id
    )
    if not is_admin:
        # Non-admins see their own requests only
        q = q.where(ResourceGrantPending.requester_user_id == user.id)
    if status:
        q = q.where(ResourceGrantPending.status == status)

    rows = (await db.execute(q.order_by(ResourceGrantPending.created_at.desc()))).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "requester_user_id": r.requester_user_id,
                "requested_capabilities": r.requested_capabilities,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
