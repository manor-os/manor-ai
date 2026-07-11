"""Workspace service — operating model, agent mapping, and activity logging."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceActivity,
)
from packages.core.services.entity_service import get_workspace


# ── Operating Model Management ──


async def get_workspace_full(
    db: AsyncSession, workspace_id: str, entity_id: str
) -> Optional[dict]:
    """Get workspace with full operating model details."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    # Fetch agent mappings for this workspace
    mappings = await get_workspace_agent_mappings(db, workspace_id, entity_id)

    om = ws.operating_model or {}
    return {
        "id": ws.id,
        "entity_id": ws.entity_id,
        "name": ws.name,
        "description": ws.description,
        "category": ws.category,
        "address": ws.address,
        "kind": ws.kind,
        "operating_context": ws.operating_context,
        "primary_work": ws.primary_work,
        "operating_model": om,
        "settings": ws.settings,
        "status": ws.status,
        "agent_mappings": mappings,
        "created_at": ws.created_at.isoformat() if ws.created_at else None,
        "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
    }


async def update_operating_model(
    db: AsyncSession, workspace_id: str, entity_id: str, operating_model: dict
) -> Optional[Workspace]:
    """Update the operating model (services, goals, rules, automations, evaluation)."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None
    ws.operating_model = operating_model
    from packages.core.workspace_chat.context import invalidate
    invalidate(workspace_id)
    await db.flush()
    return ws


async def add_workspace_service(
    db: AsyncSession, workspace_id: str, entity_id: str, service: dict
) -> Optional[Workspace]:
    """Add a service to the operating model.

    Each service must have a 'key' field used as its identifier.
    """
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    services = list(om.get("services", []))

    key = service.get("key")
    if not key:
        return None

    # Replace if exists, otherwise append
    services = [s for s in services if s.get("key") != key]
    services.append(service)

    om["services"] = services
    ws.operating_model = om
    await db.flush()
    return ws


async def remove_workspace_service(
    db: AsyncSession, workspace_id: str, entity_id: str, service_key: str
) -> Optional[Workspace]:
    """Remove a service from the operating model."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    services = list(om.get("services", []))
    om["services"] = [s for s in services if s.get("key") != service_key]
    ws.operating_model = om
    await db.flush()
    return ws


async def update_workspace_goals(
    db: AsyncSession, workspace_id: str, entity_id: str, goals: list[dict]
) -> Optional[Workspace]:
    """Update workspace goals."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    om["goals"] = goals
    ws.operating_model = om
    await db.flush()
    return ws


async def update_workspace_rules(
    db: AsyncSession, workspace_id: str, entity_id: str, rules: list[dict]
) -> Optional[Workspace]:
    """Update workspace rules."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    om["rules"] = rules
    ws.operating_model = om
    from packages.core.workspace_chat.context import invalidate
    invalidate(workspace_id)
    await db.flush()
    return ws


async def update_workspace_automations(
    db: AsyncSession, workspace_id: str, entity_id: str, automations: list[dict]
) -> Optional[Workspace]:
    """Update workspace automations."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    om["automations"] = automations
    ws.operating_model = om
    await db.flush()
    return ws


async def update_workspace_evaluation(
    db: AsyncSession, workspace_id: str, entity_id: str, evaluation: dict
) -> Optional[Workspace]:
    """Update workspace evaluation scorecard."""
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        return None

    om = dict(ws.operating_model or {})
    om["evaluation"] = evaluation
    ws.operating_model = om
    await db.flush()
    return ws


# ── Agent Mapping ──


async def map_agent_to_service(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    service_key: str,
    agent_id: str,
    custom_prompt: str = None,
) -> AgentSubscription:
    """Bind an agent to a workspace service.

    If an active subscription already exists for the same workspace + service_key,
    it is updated rather than duplicated.
    """
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        raise ValueError("Workspace not found")

    agent = await db.get(Agent, agent_id)
    if not agent or agent.deleted_at is not None or agent.status != "active":
        raise ValueError("Agent not found")
    if agent.entity_id and agent.entity_id != entity_id and not agent.is_template:
        raise ValueError("Agent belongs to another entity")

    # Look for existing subscription for this service_key
    result = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == service_key,
            AgentSubscription.status == "active",
        )
    )
    sub = result.scalar_one_or_none()

    if sub:
        sub.agent_id = agent_id
        if custom_prompt is not None:
            sub.custom_prompt = custom_prompt
    else:
        sub = AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            service_key=service_key,
            custom_prompt=custom_prompt,
        )
        db.add(sub)

    await db.flush()
    return sub


async def unmap_agent_from_service(
    db: AsyncSession, workspace_id: str, entity_id: str, service_key: str
) -> bool:
    """Remove agent binding from a workspace service."""
    result = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == service_key,
            AgentSubscription.status == "active",
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return False

    sub.status = "inactive"
    await db.flush()
    return True


async def get_workspace_agent_mappings(
    db: AsyncSession, workspace_id: str, entity_id: str
) -> list[dict]:
    """Get all agent-to-service mappings for a workspace."""
    result = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )
    subs = result.scalars().all()
    return [
        {
            "id": s.id,
            "agent_id": s.agent_id,
            "service_key": s.service_key,
            "custom_prompt": s.custom_prompt,
            "config": s.config,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in subs
    ]


# ── Activity Logging ──


async def record_activity(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    event_type: str,
    summary: str,
    details: dict = None,
    user_id: str = None,
    agent_id: str = None,
) -> None:
    """Record a workspace activity event."""
    activity = WorkspaceActivity(
        id=generate_ulid(),
        workspace_id=workspace_id,
        entity_id=entity_id,
        event_type=event_type,
        summary=summary,
        details=details,
        user_id=user_id,
        agent_id=agent_id,
    )
    db.add(activity)
    await db.flush()


def _activity_details(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _activity_task_ids(details: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    raw_ids = details.get("task_ids")
    if isinstance(raw_ids, list):
        ids.extend(str(task_id).strip() for task_id in raw_ids if str(task_id).strip())
    raw_id = details.get("task_id")
    if raw_id:
        ids.append(str(raw_id).strip())

    seen: set[str] = set()
    unique: list[str] = []
    for task_id in ids:
        if task_id not in seen:
            seen.add(task_id)
            unique.append(task_id)
    return unique


def _output_file_path(file: Any) -> str:
    if not isinstance(file, dict):
        return ""
    value = (
        file.get("fs_path")
        or file.get("saved_to")
        or file.get("path")
        or file.get("file_url")
        or file.get("document_url")
        or file.get("result_url")
        or file.get("output_url")
        or file.get("url")
    )
    return str(value or "").strip()


def _output_file_label(file: Any, fallback: str = "Generated file") -> str:
    if not isinstance(file, dict):
        return fallback
    value = (
        file.get("name")
        or file.get("filename")
        or file.get("original_name")
        or file.get("title")
        or _output_file_path(file)
        or file.get("document_id")
    )
    text = str(value or "").strip()
    if not text:
        return fallback
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    return parts[-1] if parts else text


def _collect_output_files(actual_output: Any) -> list[dict[str, Any]]:
    if not isinstance(actual_output, dict):
        return []

    raw_files: list[Any] = []
    files = actual_output.get("files")
    if isinstance(files, list):
        raw_files.extend(files)
    for step in actual_output.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_files = step.get("files")
        if isinstance(step_files, list):
            for file in step_files:
                if isinstance(file, dict) and not file.get("step") and step.get("key"):
                    raw_files.append({**file, "step": step.get("key")})
                else:
                    raw_files.append(file)

    seen: set[str] = set()
    collected: list[dict[str, Any]] = []
    for idx, file in enumerate(raw_files):
        if not isinstance(file, dict):
            continue
        path = _output_file_path(file)
        document_id = str(file.get("document_id") or file.get("doc_id") or "").strip()
        external_url = str(file.get("public_url") or file.get("url") or "").strip()
        key = document_id or path or external_url or f"file:{idx}"
        if key in seen:
            continue
        seen.add(key)
        collected.append({
            "label": _output_file_label(file, f"File {idx + 1}"),
            "fs_path": path or None,
            "document_id": document_id or None,
            "external_url": external_url or None,
            "step": file.get("step") or file.get("step_key") or file.get("key"),
        })
    return collected


def _activity_summary_with_tasks(
    event_type: str,
    summary: str,
    task_summaries: list[dict[str, Any]],
) -> str:
    if not task_summaries:
        return summary
    title = str(task_summaries[0].get("title") or "Untitled task")
    extra = len(task_summaries) - 1
    if event_type == "workspace_work_batch.completed":
        if extra <= 0:
            return f"Task completed: {title}"
        return f"Workspace task wave completed: {title} + {extra} more"
    if event_type == "workspace_work_batch.started":
        if extra <= 0:
            return f"Task wave started: {title}"
        return f"Task wave started: {title} + {extra} more"
    if event_type == "strategist_proposal.approved":
        if extra <= 0:
            return f"Approved strategist task: {title}"
        return f"Approved strategist tasks: {title} + {extra} more"
    return summary


def _user_display_name(user: User | None) -> str | None:
    if not user:
        return None
    full_name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        if part
    ).strip()
    return getattr(user, "display_name", None) or full_name or getattr(user, "email", None)


async def list_activity(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    limit: int = 50,
    event_type: str = None,
) -> list[dict]:
    """List recent workspace activity."""
    q = (
        select(WorkspaceActivity)
        .where(
            WorkspaceActivity.workspace_id == workspace_id,
            WorkspaceActivity.entity_id == entity_id,
        )
    )
    if event_type:
        q = q.where(WorkspaceActivity.event_type == event_type)

    q = q.order_by(desc(WorkspaceActivity.created_at)).limit(limit)

    result = await db.execute(q)
    rows = result.scalars().all()

    user_ids = list(dict.fromkeys(row.user_id for row in rows if row.user_id))
    users_by_id: dict[str, User] = {}
    if user_ids:
        users = (await db.execute(
            select(User).where(User.id.in_(user_ids), User.deleted_at.is_(None))
        )).scalars().all()
        users_by_id = {user.id: user for user in users}

    agent_ids = list(dict.fromkeys(row.agent_id for row in rows if row.agent_id))
    agents_by_id: dict[str, Agent] = {}
    if agent_ids:
        direct_agents = (await db.execute(
            select(Agent).where(Agent.id.in_(agent_ids))
        )).scalars().all()
        agents_by_id.update({agent.id: agent for agent in direct_agents})

        subs = (await db.execute(
            select(AgentSubscription).where(AgentSubscription.id.in_(agent_ids))
        )).scalars().all()
        sub_agent_ids = list(dict.fromkeys(sub.agent_id for sub in subs if sub.agent_id))
        if sub_agent_ids:
            sub_agents = (await db.execute(
                select(Agent).where(Agent.id.in_(sub_agent_ids))
            )).scalars().all()
            sub_agents_by_id = {agent.id: agent for agent in sub_agents}
            for sub in subs:
                if agent := sub_agents_by_id.get(sub.agent_id):
                    agents_by_id[sub.id] = agent

    task_ids: list[str] = []
    for row in rows:
        task_ids.extend(_activity_task_ids(_activity_details(row.details)))
    task_ids = list(dict.fromkeys(task_ids))

    tasks_by_id: dict[str, Any] = {}
    doc_ids_by_path: dict[str, str] = {}
    if task_ids:
        from packages.core.models.document import Document
        from packages.core.models.task import Task

        task_rows = (await db.execute(
            select(Task).where(
                Task.entity_id == entity_id,
                Task.workspace_id == workspace_id,
                Task.id.in_(task_ids),
            )
        )).scalars().all()
        tasks_by_id = {task.id: task for task in task_rows}

        paths = list(dict.fromkeys(
            file["fs_path"]
            for task in task_rows
            for file in _collect_output_files(task.actual_output)
            if file.get("fs_path")
        ))
        if paths:
            doc_rows = (await db.execute(
                select(Document.id, Document.fs_path).where(
                    Document.entity_id == entity_id,
                    Document.fs_path.in_(paths),
                )
            )).all()
            doc_ids_by_path = {
                str(row.fs_path): row.id
                for row in doc_rows
                if row.fs_path
            }

    def task_summary(task_id: str) -> dict[str, Any] | None:
        task = tasks_by_id.get(task_id)
        if task is None:
            return None
        files = _collect_output_files(task.actual_output)
        for file in files:
            path = file.get("fs_path")
            if path and not file.get("document_id"):
                file["document_id"] = doc_ids_by_path.get(str(path))
        return {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "owner_service_key": task.owner_service_key,
            "owner_subscription_id": task.owner_subscription_id,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "files": files,
        }

    enriched: list[dict[str, Any]] = []
    for row in rows:
        details = dict(_activity_details(row.details))
        summaries = [
            summary
            for task_id in _activity_task_ids(details)
            if (summary := task_summary(task_id)) is not None
        ]
        if summaries:
            details["task_summaries"] = summaries
            details.setdefault("primary_task", summaries[0])
        user = users_by_id.get(row.user_id or "")
        agent = agents_by_id.get(row.agent_id or "")
        user_name = _user_display_name(user)
        agent_name = getattr(agent, "name", None)
        actor_type = "user" if user else "agent" if agent else None
        actor_id = row.user_id if user else row.agent_id if agent else None
        actor_name = user_name or agent_name
        enriched.append({
            "id": row.id,
            "event_type": row.event_type,
            "summary": _activity_summary_with_tasks(row.event_type, row.summary, summaries),
            "details": details,
            "user_id": row.user_id,
            "user_name": user_name,
            "user_email": getattr(user, "email", None) if user else None,
            "user_avatar_url": getattr(user, "avatar_url", None) if user else None,
            "agent_id": row.agent_id,
            "agent_name": agent_name,
            "agent_avatar_url": getattr(agent, "avatar_url", None) if agent else None,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    return enriched
