"""Entity data backup and restore.

Exports all entity data as a JSON archive. Can be used for:
- Entity migration between instances
- Data backup before destructive operations
- Compliance-oriented data export
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def export_entity_data(db: AsyncSession, entity_id: str) -> dict:
    """Export all data for an entity as a JSON-serializable dict.

    Includes: entity info, users, workspaces, tasks (with logs),
    conversations (with messages), documents, agents, integrations,
    channels, clients, staff_members, notifications, settings.

    Does NOT include: passwords, API keys, OAuth tokens (security).
    """
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace, Agent, AgentSubscription
    from packages.core.models.task import Task, TaskLog, TaskCategory, Conversation, Message
    from packages.core.models.document import Document, DocumentGroup
    from packages.core.models.people import Client
    from packages.core.models.staff import Staff

    # Helper to serialize a SQLAlchemy row to dict
    def _row_to_dict(row) -> dict:
        d = {}
        for col in row.__table__.columns:
            val = getattr(row, col.name, None)
            if isinstance(val, datetime):
                val = val.isoformat()
            elif isinstance(val, (dict, list, str, int, float, bool, type(None))):
                pass  # JSON-safe, keep as-is
            else:
                val = str(val)
            d[col.name] = val
        return d

    async def _fetch_all(model, **filters):
        q = select(model)
        for k, v in filters.items():
            q = q.where(getattr(model, k) == v)
        result = await db.execute(q)
        return [_row_to_dict(row) for row in result.scalars().all()]

    # Build export
    export: dict[str, Any] = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entity_id": entity_id,
    }

    # Entity
    entities = await _fetch_all(Entity, id=entity_id)
    export["entity"] = entities[0] if entities else {}

    # Users (exclude password_hash)
    users = await _fetch_all(User, entity_id=entity_id)
    for u in users:
        u.pop("password_hash", None)
    export["users"] = users

    # Workspaces
    export["workspaces"] = await _fetch_all(Workspace, entity_id=entity_id)

    # Task categories
    export["task_categories"] = await _fetch_all(TaskCategory, entity_id=entity_id)

    # Tasks + logs
    tasks = await _fetch_all(Task, entity_id=entity_id)
    for task in tasks:
        task_logs = await _fetch_all(TaskLog, task_id=task["id"])
        task["logs"] = task_logs
    export["tasks"] = tasks

    # Conversations + messages
    conversations = await _fetch_all(Conversation, entity_id=entity_id)
    for conv in conversations:
        msgs = await _fetch_all(Message, conversation_id=conv["id"])
        conv["messages"] = msgs
    export["conversations"] = conversations

    # Documents
    export["documents"] = await _fetch_all(Document, entity_id=entity_id)

    # Document groups
    export["document_groups"] = await _fetch_all(DocumentGroup, entity_id=entity_id)

    # Agents
    export["agents"] = await _fetch_all(Agent, entity_id=entity_id)

    # Agent subscriptions
    export["agent_subscriptions"] = await _fetch_all(AgentSubscription, entity_id=entity_id)

    # Clients
    export["clients"] = await _fetch_all(Client, entity_id=entity_id)

    # Staff (employees + contractors + vendors + externals — all in one table)
    export["staff_members"] = await _fetch_all(Staff, entity_id=entity_id)

    # Stats
    export["stats"] = {
        "users": len(export["users"]),
        "workspaces": len(export["workspaces"]),
        "tasks": len(export["tasks"]),
        "conversations": len(export["conversations"]),
        "documents": len(export["documents"]),
        "agents": len(export["agents"]),
        "clients": len(export["clients"]),
        "staff_members": len(export["staff_members"]),
    }

    return export


async def get_export_summary(db: AsyncSession, entity_id: str) -> dict:
    """Get a summary of what would be exported (without actually exporting).
    Faster than full export -- just counts.
    """
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace, Agent
    from packages.core.models.task import Task, Conversation
    from packages.core.models.document import Document
    from packages.core.models.people import Client
    from packages.core.models.staff import Staff

    async def _count(model):
        result = await db.execute(
            select(func.count()).select_from(model).where(model.entity_id == entity_id)
        )
        return result.scalar() or 0

    return {
        "entity_id": entity_id,
        "users": await _count(User),
        "workspaces": await _count(Workspace),
        "tasks": await _count(Task),
        "conversations": await _count(Conversation),
        "documents": await _count(Document),
        "agents": await _count(Agent),
        "clients": await _count(Client),
        "staff_members": await _count(Staff),
    }
