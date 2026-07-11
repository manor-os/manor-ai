"""Client portal — external-facing service for client self-service.

Clients authenticate with a portal token (generated per client).
"""
import secrets
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.people import Client
from packages.core.models.task import Task

logger = logging.getLogger(__name__)


async def generate_portal_token(db: AsyncSession, client_id: str, entity_id: str) -> str:
    """Generate a portal access token for a client. Stored in client metadata."""
    result = await db.execute(
        select(Client).where(Client.id == client_id, Client.entity_id == entity_id)
    )
    client = result.scalar_one_or_none()
    if not client:
        raise ValueError("Client not found")

    token = secrets.token_urlsafe(32)
    meta = dict(client.meta) if client.meta else {}
    meta["portal_token"] = token
    client.meta = meta
    await db.flush()
    return token


async def revoke_portal_token(db: AsyncSession, client_id: str, entity_id: str) -> bool:
    """Revoke portal access token for a client."""
    result = await db.execute(
        select(Client).where(Client.id == client_id, Client.entity_id == entity_id)
    )
    client = result.scalar_one_or_none()
    if not client:
        return False

    meta = dict(client.meta) if client.meta else {}
    meta.pop("portal_token", None)
    client.meta = meta
    await db.flush()
    return True


async def authenticate_portal(db: AsyncSession, token: str) -> Client | None:
    """Authenticate a client by their portal token."""
    # Search all clients for matching token in metadata
    result = await db.execute(select(Client))
    for client in result.scalars().all():
        meta = client.meta or {}
        if meta.get("portal_token") == token:
            return client
    return None


async def submit_ticket(db: AsyncSession, client_id: str, entity_id: str, title: str, description: str = None, *, details: dict = None) -> Task:
    """Client submits a support ticket (creates a task)."""
    from packages.core.services.task_service import create_task

    task = await create_task(
        db, entity_id,
        title=title,
        description=description,
        task_type="client_ticket",
        details={
            **(details or {}),
            "submitted_by_client": client_id,
            "source": "portal",
        },
    )
    return task


async def list_client_tickets(db: AsyncSession, client_id: str, entity_id: str) -> list[Task]:
    """List tickets submitted by a client."""
    result = await db.execute(
        select(Task)
        .where(
            Task.entity_id == entity_id,
            Task.details["submitted_by_client"].astext == client_id,
        )
        .order_by(Task.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().all())


async def get_client_ticket(db: AsyncSession, ticket_id: str, client_id: str, entity_id: str) -> Task | None:
    """Get a specific ticket — only if submitted by this client."""
    result = await db.execute(
        select(Task).where(
            Task.id == ticket_id,
            Task.entity_id == entity_id,
            Task.details["submitted_by_client"].astext == client_id,
        )
    )
    return result.scalar_one_or_none()


async def add_ticket_comment(db: AsyncSession, ticket_id: str, client_id: str, entity_id: str, content: str) -> dict:
    """Client adds a comment to their ticket."""
    ticket = await get_client_ticket(db, ticket_id, client_id, entity_id)
    if not ticket:
        raise ValueError("Ticket not found")

    from packages.core.services.task_service import add_task_log
    log = await add_task_log(
        db, ticket_id,
        log_type="client_comment",
        content=content,
        created_by=f"client:{client_id}",
    )
    return {"id": log.id, "content": content, "created_at": log.created_at.isoformat() if log.created_at else None}
