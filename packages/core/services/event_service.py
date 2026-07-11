"""Event log service — log events and build activity feeds."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.event import EventLog
from packages.core.models.base import generate_ulid


async def log_event(
    db: AsyncSession,
    entity_id: str,
    event_type: str,
    source: Optional[str] = None,
    payload: Optional[dict] = None,
) -> EventLog:
    """Log an event."""
    entry = EventLog(
        id=generate_ulid(),
        entity_id=entity_id,
        event_type=event_type,
        source=source,
        payload=payload or {},
    )
    db.add(entry)
    await db.flush()
    return entry


async def list_events(
    db: AsyncSession,
    entity_id: str,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[EventLog], int]:
    """List events with optional filters."""
    base = select(EventLog).where(EventLog.entity_id == entity_id)
    count_q = select(func.count()).select_from(EventLog).where(EventLog.entity_id == entity_id)

    if event_type:
        base = base.where(EventLog.event_type == event_type)
        count_q = count_q.where(EventLog.event_type == event_type)
    if source:
        base = base.where(EventLog.source == source)
        count_q = count_q.where(EventLog.source == source)

    total = (await db.execute(count_q)).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(EventLog.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return list(rows), total


# ── Activity feed description builders ──

_EVENT_DESCRIPTIONS: dict[str, callable] = {}


def _desc(event_type: str):
    """Decorator to register a description builder for an event type."""
    def decorator(fn):
        _EVENT_DESCRIPTIONS[event_type] = fn
        return fn
    return decorator


_EVENT_ICONS = {
    "task": "clipboard",
    "document": "file",
    "agent": "bot",
    "user": "user",
    "goal": "target",
}


def _icon_for(event_type: str) -> str:
    prefix = event_type.split(".")[0] if "." in event_type else event_type
    return _EVENT_ICONS.get(prefix, "activity")


def _link_for(event_type: str, payload: dict) -> Optional[str]:
    """Generate an optional frontend link path from the event."""
    if event_type.startswith("task.") and "task_id" in payload:
        return f"/tasks/{payload['task_id']}"
    if event_type.startswith("document.") and "document_id" in payload:
        return f"/documents/{payload['document_id']}"
    if event_type.startswith("agent.") and "agent_id" in payload:
        return f"/agents/{payload['agent_id']}"
    return None


@_desc("task.created")
def _task_created(p: dict) -> str:
    return f"New task: {p.get('title', 'Untitled')}"


@_desc("task.status_changed")
def _task_status_changed(p: dict) -> str:
    title = p.get("title", p.get("task_id", "Unknown"))
    return f"Task '{title}' moved to {p.get('new_status', '?')}"


@_desc("document.uploaded")
def _doc_uploaded(p: dict) -> str:
    return f"Document '{p.get('name', 'Untitled')}' uploaded"


@_desc("agent.created")
def _agent_created(p: dict) -> str:
    return f"New agent: {p.get('name', 'Unnamed')}"


@_desc("user.login")
def _user_login(p: dict) -> str:
    return f"{p.get('username', 'Someone')} logged in"


@_desc("goal.started")
def _goal_started(p: dict) -> str:
    return f"Goal started: {p.get('goal', 'Untitled')}"


@_desc("goal.completed")
def _goal_completed(p: dict) -> str:
    return f"Goal completed: {p.get('goal', 'Untitled')}"


def _build_description(event_type: str, payload: dict) -> str:
    builder = _EVENT_DESCRIPTIONS.get(event_type)
    if builder:
        return builder(payload)
    # Fallback: humanize event_type
    return event_type.replace(".", " ").replace("_", " ").title()


async def get_activity_feed(
    db: AsyncSession,
    entity_id: str,
    limit: int = 20,
) -> list[dict]:
    """Build a human-readable activity feed from event logs.

    Returns: [{
        id, event_type, source, description, timestamp,
        icon (emoji/type hint for frontend), link (optional URL path)
    }, ...]
    """
    result = await db.execute(
        select(EventLog)
        .where(EventLog.entity_id == entity_id)
        .order_by(EventLog.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()

    feed = []
    for e in rows:
        payload = e.payload or {}
        feed.append({
            "id": e.id,
            "event_type": e.event_type,
            "source": e.source,
            "description": _build_description(e.event_type, payload),
            "timestamp": e.created_at.isoformat(),
            "icon": _icon_for(e.event_type),
            "link": _link_for(e.event_type, payload),
        })

    return feed
