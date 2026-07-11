"""Permission-v1 endpoints — knowledge-base classification, legal hold,
visibility changes, and access requests.

Reference implementation showing how routers should call the new
``authorize()`` entry. Behavior is fully gated by ``permissions_v1_enforce``;
when the flag is OFF (default), endpoints fall back to the legacy
``require_permission`` dependency chain — no behavior change for anyone
currently in production.

Sister-RFC: ``docs/PERMISSIONS_DESIGN_ZH.md`` §13.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from apps.api.errors import CodedError
from packages.core.auth import (
    Resource,
    authorize,
    make_actor,
    require,
)
from packages.core.database import get_db
from packages.core.models import (
    Capability,
    Classification,
    ResourceType,
    Visibility,
)
from packages.core.models.document import Document
from packages.core.models.permission import (
    PendingStatus,
    ResourceGrantPending,
)
from packages.core.models.user import User
from packages.core.permissions import Permission

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


class LegalHoldRequest(BaseModel):
    enabled: bool
    reason: Optional[str] = None


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


async def _load_doc(db: AsyncSession, document_id: str) -> Document:
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise CodedError(
            404,
            code="permissions.error.doc.not_found",
            message="Document not found",
        )
    return doc


def _doc_to_resource(doc: Document) -> Resource:
    return Resource(
        type=ResourceType.DOCUMENT,
        id=doc.id,
        entity_id=doc.entity_id,
        workspace_id=None,  # not modeled on Document directly today
        visibility=getattr(doc, "visibility", None),
        classification=getattr(doc, "classification", None),
        owner_id=getattr(doc, "owner_id", None),
        client_visible=bool(getattr(doc, "client_visible", False)),
        legal_hold=bool(getattr(doc, "legal_hold", False)),
        quarantine_status=getattr(doc, "quarantine_status", None),
    )


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/documents/{document_id}/classify")
async def reclassify_document(
    document_id: str,
    body: ClassifyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a document's classification.

    Authorization: caller must hold the ``reclassify`` capability on the
    resource OR be an admin (legacy fallthrough). Restricted/legal-hold
    invariants are enforced by ``authorize()``.
    """
    if body.classification not in _VALID_CLASSIFICATIONS:
        raise HTTPException(
            400,
            f"Invalid classification (allowed: {sorted(_VALID_CLASSIFICATIONS)})",
        )
    doc = await _load_doc(db, document_id)
    actor = make_actor(user)
    await require(db, actor, Capability.RECLASSIFY, _doc_to_resource(doc))

    # Refuse silent classification *downgrade* — must explicitly use
    # admin.audit verb to drop confidentiality. (Invariant 13.3.)
    current = getattr(doc, "classification", Classification.INTERNAL)
    if Classification.rank(body.classification) < Classification.rank(current):
        await require(db, actor, Permission.ADMIN_AUDIT)

    doc.classification = body.classification
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.visibility not in _VALID_VISIBILITIES:
        raise HTTPException(
            400,
            f"Invalid visibility (allowed: {sorted(_VALID_VISIBILITIES)})",
        )
    doc = await _load_doc(db, document_id)
    actor = make_actor(user)
    await require(db, actor, Capability.MANAGE_METADATA, _doc_to_resource(doc))

    # Invariant 1: restricted ⇒ visibility ≠ public
    if (
        getattr(doc, "classification", None) == Classification.RESTRICTED
        and body.visibility == Visibility.PUBLIC
    ):
        raise HTTPException(
            409, "restricted documents cannot have public visibility"
        )

    doc.visibility = body.visibility
    await db.commit()
    return {"id": doc.id, "visibility": doc.visibility}


@router.post("/documents/{document_id}/legal-hold")
async def toggle_legal_hold(
    document_id: str,
    body: LegalHoldRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Place / lift a legal hold on a document.

    Once held, ``authorize()`` blocks delete / reclassify / retention
    triggers via invariant 5 — see RFC §13.9.
    """
    doc = await _load_doc(db, document_id)
    actor = make_actor(user)
    await require(db, actor, Capability.LEGAL_HOLD, _doc_to_resource(doc))

    doc.legal_hold = body.enabled
    if body.enabled:
        if not body.reason:
            raise CodedError(
                400,
                code="permissions.error.legal_hold.reason_required",
                message="reason required when enabling legal hold",
            )
        doc.legal_hold_reason = body.reason
        doc.legal_hold_set_by = user.id
        doc.legal_hold_set_at = datetime.now(timezone.utc)
    else:
        doc.legal_hold_reason = None
        doc.legal_hold_set_by = None
        doc.legal_hold_set_at = None

    await db.commit()
    return {"id": doc.id, "legal_hold": doc.legal_hold}


@router.post("/documents/{document_id}/client-visible")
async def set_document_client_visible(
    document_id: str,
    body: ClientVisibleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle ``client_visible`` on a document.

    Same authorization as visibility/classification (``MANAGE_METADATA``).
    Refuses if the document is Confidential or Restricted — those
    classifications imply the client portal must not surface the doc
    (invariant 4, RFC §13.14). The frontend disables the toggle in those
    cases, but the server gate is the source of truth.
    """
    doc = await _load_doc(db, document_id)
    actor = make_actor(user)
    await require(db, actor, Capability.MANAGE_METADATA, _doc_to_resource(doc))

    cls = getattr(doc, "classification", None)
    if body.client_visible and cls in (Classification.CONFIDENTIAL, Classification.RESTRICTED):
        raise CodedError(
            409,
            code="permissions.error.doc.client_visible_blocked_by_classification",
            message="Confidential/Restricted documents cannot be client-visible",
        )

    doc.client_visible = body.client_visible
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
    actor = make_actor(user)
    decision = await authorize(db, actor, Permission.ADMIN_AUDIT)
    is_admin = decision.allow

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
