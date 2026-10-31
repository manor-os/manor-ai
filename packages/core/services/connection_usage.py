"""Owner-visible record of other members using a shared connection.

Recording sits on the tool-dispatch hot path, and every transaction takes a
database admission permit. So callers only enqueue: one writer task per event
loop drains the queue, inserting everything that arrived meanwhile in a single
statement. A burst of N calls therefore costs about one transaction, not N,
and never delays the call that is being recorded.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import weakref
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.connection_use import IntegrationConnectionUse
from packages.core.models.user import User

logger = logging.getLogger(__name__)

# Bound memory if the database stalls; dropped rows are only observability.
MAX_PENDING_USES = 5000


@dataclass
class _UseWriter:
    pending: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    dropped: int = 0


_writers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _UseWriter]" = weakref.WeakKeyDictionary()


def record_shared_connection_use(
    *,
    entity_id: str,
    connection_kind: str,
    connection_id: str,
    owner_user_id: str | None,
    actor_user_id: str | None,
    server_key: str,
    tool_name: str,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> None:
    """Queue one non-owner use for the owner's usage view; never blocks.

    The owner's own use is skipped. Without a running event loop the row is
    dropped, as with every other fire-and-forget observability write.
    """
    if not owner_user_id or not actor_user_id or owner_user_id == actor_user_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    writer = _writers.get(loop)
    if writer is None:
        writer = _writers[loop] = _UseWriter()
    if len(writer.pending) >= MAX_PENDING_USES:
        writer.dropped += 1
        if writer.dropped % 1000 == 1:
            logger.warning("Shared connection use queue full; %d uses dropped", writer.dropped)
        return
    writer.pending.append({
        "id": generate_ulid(),
        "entity_id": entity_id,
        "connection_kind": connection_kind,
        "connection_id": connection_id,
        "owner_user_id": owner_user_id,
        "actor_user_id": actor_user_id,
        # ULID columns: an over-long id is dropped rather than failing the batch.
        "agent_id": agent_id if agent_id and len(agent_id) <= 26 else None,
        "conversation_id": conversation_id if conversation_id and len(conversation_id) <= 26 else None,
        "server_key": server_key[:80],
        "tool_name": tool_name[:120],
    })
    if writer.task is None or writer.task.done():
        # A fresh context: the writer must not inherit the enqueuing turn's
        # database admission lineage, or its inserts would ride that turn's
        # permit and bypass backpressure. Holding nothing, it waits safely.
        writer.task = loop.create_task(_drain(writer), context=contextvars.Context())


async def _drain(writer: _UseWriter) -> None:
    from packages.core import database

    while writer.pending:
        batch, writer.pending = writer.pending, []
        try:
            async with database.async_session() as db:
                await db.execute(insert(IntegrationConnectionUse), batch)
                await db.commit()
        except Exception:
            logger.warning(
                "%d shared connection uses were not recorded", len(batch), exc_info=True,
            )


async def flush_shared_connection_uses() -> None:
    """Wait until this loop's queued uses are written (tests and shutdown)."""
    writer = _writers.get(asyncio.get_running_loop())
    while writer is not None and writer.task is not None and not writer.task.done():
        await asyncio.shield(writer.task)


@dataclass(frozen=True)
class ConnectionUse:
    actor_user_id: str
    actor_display_name: str | None
    server_key: str
    tool_name: str
    created_at: datetime


async def list_connection_uses(
    db: AsyncSession,
    *,
    entity_id: str,
    connection_kind: str,
    connection_id: str,
    limit: int = 50,
) -> list[ConnectionUse]:
    rows = (await db.execute(
        select(IntegrationConnectionUse, User.display_name, User.email)
        .outerjoin(User, User.id == IntegrationConnectionUse.actor_user_id)
        .where(
            IntegrationConnectionUse.entity_id == entity_id,
            IntegrationConnectionUse.connection_kind == connection_kind,
            IntegrationConnectionUse.connection_id == connection_id,
        )
        .order_by(IntegrationConnectionUse.created_at.desc(), IntegrationConnectionUse.id.desc())
        .limit(limit)
    )).all()
    return [
        ConnectionUse(
            actor_user_id=use.actor_user_id,
            actor_display_name=display_name or email,
            server_key=use.server_key,
            tool_name=use.tool_name,
            created_at=use.created_at,
        )
        for use, display_name, email in rows
    ]
