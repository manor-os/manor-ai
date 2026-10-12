"""Bulk operations — batch update/delete, CSV export/import."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.document import Document
from packages.core.models.people import Client
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.comment_service import delete_resource_comments


# ── Bulk task operations ──

async def bulk_update_tasks(
    db: AsyncSession, entity_id: str,
    task_ids: list[str], updates: dict,
) -> int:
    """Update multiple tasks at once. Returns count updated."""
    if not task_ids or not updates:
        return 0
    # Only allow safe fields
    allowed = {"status", "priority", "assignee_id"}
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return 0
    from packages.core.services.task_service import update_task

    # Stable ordering keeps concurrent overlapping bulk requests on one lock
    # order. Every row enters the canonical Task state machine/factory so an
    # in_progress Workspace Task gets its durable execution intent in this
    # transaction instead of becoming a stranded raw status update.
    matching_ids = list((await db.execute(
        select(Task.id)
        .where(Task.id.in_(task_ids), Task.entity_id == entity_id)
        .order_by(Task.id.asc())
    )).scalars())
    updated_count = 0
    for task_id in matching_ids:
        if await update_task(db, task_id, entity_id, **clean) is not None:
            updated_count += 1
    if updated_count:
        await bump_tool_cache_version(entity_id, "documents")
    return updated_count


async def bulk_delete_documents(
    db: AsyncSession, entity_id: str,
    document_ids: list[str],
) -> int:
    """Delete multiple documents. Returns count deleted."""
    if not document_ids:
        return 0
    matching_ids = set((await db.execute(
        select(Document.id)
        .where(
            Document.id.in_(document_ids),
            Document.entity_id == entity_id,
        )
        .order_by(Document.id)
        .with_for_update()
    )).scalars().all())
    await delete_resource_comments(db, entity_id, "document", matching_ids)
    result = await db.execute(
        delete(Document)
        .where(Document.id.in_(document_ids), Document.entity_id == entity_id)
    )
    await db.flush()
    return result.rowcount


async def bulk_update_task_status(
    db: AsyncSession, entity_id: str,
    task_ids: list[str], status: str,
) -> int:
    """Change status of multiple tasks at once."""
    if not task_ids:
        return 0
    from packages.core.services.task_service import update_task

    matching_ids = list((await db.execute(
        select(Task.id)
        .where(Task.id.in_(task_ids), Task.entity_id == entity_id)
        .order_by(Task.id.asc())
    )).scalars())
    updated_count = 0
    for task_id in matching_ids:
        if (
            await update_task(db, task_id, entity_id, status=status)
            is not None
        ):
            updated_count += 1
    return updated_count


# ── CSV export ──

async def export_tasks_csv(
    db: AsyncSession, entity_id: str, *,
    status: str | None = None,
) -> str:
    """Export tasks as CSV string."""
    q = select(Task).where(Task.entity_id == entity_id)
    if status:
        q = q.where(Task.status == status)
    q = q.order_by(Task.created_at.desc())
    result = await db.execute(q)
    tasks = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "status", "priority", "task_type",
                      "assignee_id", "deadline", "created_at"])
    for t in tasks:
        writer.writerow([
            t.id, t.title, t.status, t.priority, t.task_type,
            t.assignee_id or "",
            t.deadline.isoformat() if t.deadline else "",
            t.created_at.isoformat() if t.created_at else "",
        ])
    return buf.getvalue()


async def export_clients_csv(db: AsyncSession, entity_id: str) -> str:
    """Export clients as CSV string."""
    q = select(Client).where(Client.entity_id == entity_id).order_by(Client.created_at.desc())
    result = await db.execute(q)
    clients = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "name", "email", "phone", "address", "status", "created_at"])
    for c in clients:
        writer.writerow([
            c.id, c.name, c.email or "", c.phone or "",
            c.address or "", c.status,
            c.created_at.isoformat() if c.created_at else "",
        ])
    return buf.getvalue()


# ── CSV import ──

async def import_tasks_csv(
    db: AsyncSession, entity_id: str,
    csv_content: str, creator_id: str | None = None,
) -> int:
    """Import tasks from CSV. Returns count imported.

    Expected columns: title, description, priority, status, deadline
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0
    for row in reader:
        title = row.get("title", "").strip()
        if not title:
            continue
        priority_raw = row.get("priority", "3").strip()
        try:
            priority = int(priority_raw)
        except ValueError:
            priority = 3
        deadline_raw = row.get("deadline", "").strip()
        deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
        task = Task(
            id=generate_ulid(),
            entity_id=entity_id,
            title=title,
            description=row.get("description", "").strip() or None,
            priority=priority,
            status=row.get("status", "pending").strip() or "pending",
            deadline=deadline,
            creator_id=creator_id,
        )
        db.add(task)
        count += 1
    await db.flush()
    return count
