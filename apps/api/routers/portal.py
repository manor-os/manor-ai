"""Client portal endpoints — external-facing self-service API.

Clients authenticate via X-Portal-Token header (NOT JWT).
Admin endpoints use standard JWT auth.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.people import Client
from packages.core.models.user import User
from packages.core.services.portal_service import (
    authenticate_portal,
    generate_portal_token,
    revoke_portal_token,
    submit_ticket,
    list_client_tickets,
    get_client_ticket,
    add_ticket_comment,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


# ── Schemas ──

class TicketCreateRequest(BaseModel):
    title: str
    description: str | None = None
    details: dict | None = None


class TicketCommentRequest(BaseModel):
    content: str


class TicketResponse(BaseModel):
    id: str
    entity_id: str
    title: str
    description: str | None = None
    status: str
    priority: int
    task_type: str
    created_at: str | None = None
    details: dict = {}


class CommentResponse(BaseModel):
    id: str
    content: str
    created_at: str | None = None


class ClientProfileResponse(BaseModel):
    id: str
    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    status: str = "active"


class PortalTokenResponse(BaseModel):
    token: str
    client_id: str


# ── Portal auth dependency ──

async def _get_portal_client(
    x_portal_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> Client:
    """Portal auth dependency — validates portal token."""
    client = await authenticate_portal(db, x_portal_token)
    if not client:
        raise HTTPException(401, "Invalid portal token")
    return client


# ── Helpers ──

def _ticket_response(task) -> TicketResponse:
    return TicketResponse(
        id=task.id,
        entity_id=task.entity_id,
        title=task.title,
        description=task.description,
        status=task.status,
        priority=task.priority,
        task_type=task.task_type,
        created_at=task.created_at.isoformat() if task.created_at else None,
        details=task.details or {},
    )


# ── Portal endpoints (token auth) ──

@router.post("/tickets", response_model=TicketResponse, status_code=201)
async def portal_submit_ticket(
    req: TicketCreateRequest,
    portal_client: Client = Depends(_get_portal_client),
    db: AsyncSession = Depends(get_db),
):
    """Client submits a support ticket."""
    task = await submit_ticket(
        db, portal_client.id, portal_client.entity_id,
        title=req.title, description=req.description, details=req.details,
    )
    return _ticket_response(task)


@router.get("/tickets", response_model=list[TicketResponse])
async def portal_list_tickets(
    portal_client: Client = Depends(_get_portal_client),
    db: AsyncSession = Depends(get_db),
):
    """List tickets submitted by the authenticated client."""
    tickets = await list_client_tickets(db, portal_client.id, portal_client.entity_id)
    return [_ticket_response(t) for t in tickets]


@router.get("/tickets/{ticket_id}", response_model=TicketResponse)
async def portal_get_ticket(
    ticket_id: str,
    portal_client: Client = Depends(_get_portal_client),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific ticket belonging to the authenticated client."""
    task = await get_client_ticket(db, ticket_id, portal_client.id, portal_client.entity_id)
    if not task:
        raise HTTPException(404, "Ticket not found")
    return _ticket_response(task)


@router.post("/tickets/{ticket_id}/comments", response_model=CommentResponse, status_code=201)
async def portal_add_comment(
    ticket_id: str,
    req: TicketCommentRequest,
    portal_client: Client = Depends(_get_portal_client),
    db: AsyncSession = Depends(get_db),
):
    """Client adds a comment to their ticket."""
    try:
        result = await add_ticket_comment(
            db, ticket_id, portal_client.id, portal_client.entity_id, req.content,
        )
    except ValueError:
        raise HTTPException(404, "Ticket not found")
    return CommentResponse(**result)


@router.get("/profile", response_model=ClientProfileResponse)
async def portal_profile(
    portal_client: Client = Depends(_get_portal_client),
):
    """Get the authenticated client's profile."""
    return ClientProfileResponse(
        id=portal_client.id,
        name=portal_client.name,
        email=portal_client.email,
        phone=portal_client.phone,
        address=portal_client.address,
        status=portal_client.status,
    )


# ── Admin endpoints (JWT auth) ──

@router.post("/tokens/{client_id}", response_model=PortalTokenResponse)
async def admin_generate_portal_token(
    client_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin generates a portal token for a client."""
    try:
        token = await generate_portal_token(db, client_id, user.entity_id)
    except ValueError:
        raise HTTPException(404, "Client not found")
    return PortalTokenResponse(token=token, client_id=client_id)


@router.delete("/tokens/{client_id}", status_code=204)
async def admin_revoke_portal_token(
    client_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin revokes a client's portal token."""
    ok = await revoke_portal_token(db, client_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Client not found")
