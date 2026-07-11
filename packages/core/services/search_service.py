"""Global search service — searches across tasks, documents, agents, conversations."""
from __future__ import annotations

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Task, Conversation
from packages.core.models.workspace import Agent
from packages.core.services.workspace_access import user_can_read_workspace_id


async def _workspace_scoped_result_visible(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    role: str | None,
) -> bool:
    if not workspace_id or not user_id:
        return True
    return await user_can_read_workspace_id(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        user_id=user_id,
        role=role,
    )


async def global_search(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 20,
    user_id: str | None = None,
    role: str | None = None,
) -> dict:
    """Search across multiple entity resources. Returns grouped results."""
    if not query or not query.strip():
        return {"tasks": [], "documents": [], "agents": [], "conversations": []}

    pattern = f"%{query}%"

    fetch_limit = max(limit * 4, limit)

    # Tasks — search by title and description
    task_q = (
        select(Task)
        .where(
            Task.entity_id == entity_id,
            or_(Task.title.ilike(pattern), Task.description.ilike(pattern)),
        )
        .order_by(Task.created_at.desc())
        .limit(fetch_limit)
    )
    task_result = await db.execute(task_q)
    tasks = []
    for t in task_result.scalars().all():
        if not await _workspace_scoped_result_visible(
            db,
            entity_id=entity_id,
            workspace_id=t.workspace_id,
            user_id=user_id,
            role=role,
        ):
            continue
        tasks.append({
            "id": t.id,
            "type": "task",
            "name": t.title,
            "preview": (t.description or "")[:120],
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
        if len(tasks) >= limit:
            break

    # Documents — search by name through the same visibility layer used by
    # Knowledge list/search. Direct Document queries leak private filenames.
    from packages.core.services.document_access import list_visible_documents

    doc_rows, _ = await list_visible_documents(
        db,
        entity_id,
        user_id=user_id,
        role=role,
        name_search=query,
        limit=limit,
    )
    documents = [
        {
            "id": d.id,
            "type": "document",
            "name": d.name,
            "preview": d.file_type or d.source or "",
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in doc_rows
    ]

    # Agents — search by name and description
    agent_q = (
        select(Agent)
        .where(
            Agent.entity_id == entity_id,
            Agent.deleted_at.is_(None),
            or_(Agent.name.ilike(pattern), Agent.description.ilike(pattern)),
        )
        .order_by(Agent.created_at.desc())
        .limit(limit)
    )
    agent_result = await db.execute(agent_q)
    agents = [
        {
            "id": a.id,
            "type": "agent",
            "name": a.name,
            "preview": (a.description or "")[:120],
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in agent_result.scalars().all()
    ]

    # Conversations — search by title
    conv_q = (
        select(Conversation)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.title.ilike(pattern),
        )
        .order_by(Conversation.created_at.desc())
        .limit(fetch_limit)
    )
    conv_result = await db.execute(conv_q)
    conversations = []
    for c in conv_result.scalars().all():
        if not await _workspace_scoped_result_visible(
            db,
            entity_id=entity_id,
            workspace_id=c.workspace_id,
            user_id=user_id,
            role=role,
        ):
            continue
        conversations.append({
            "id": c.id,
            "type": "conversation",
            "name": c.title or "",
            "preview": c.channel or "",
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })
        if len(conversations) >= limit:
            break

    return {
        "tasks": tasks,
        "documents": documents,
        "agents": agents,
        "conversations": conversations,
    }
