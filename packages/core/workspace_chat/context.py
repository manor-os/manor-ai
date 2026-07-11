"""Workspace context — cached summary + dynamic search for workspace chat.

The summary is a lightweight text block (~200 tokens) injected into the
chat system prompt so the LLM knows the workspace state at a glance.
The workspace_search tool lets the LLM query live details on demand.

Cache: in-memory dict with 5-min TTL. Invalidated on task/goal/proposal
changes so the chat stays fresh without re-querying on every message.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sqlalchemy import String, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────

_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 300  # 5 minutes


def _compact_patterns(patterns: list[str], *, max_items: int = 5) -> str:
    if len(patterns) <= max_items:
        return ", ".join(patterns)
    return ", ".join(patterns[:max_items]) + f", +{len(patterns) - max_items} more"


_TASK_STATUS_ALIASES = {
    "running": "in_progress",
    "run": "in_progress",
    "active": "in_progress",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "doing": "in_progress",
    "working": "in_progress",
    "进行中": "in_progress",
    "运行中": "in_progress",
    "执行中": "in_progress",
    "正在运行": "in_progress",
    "todo": "pending",
    "to do": "pending",
    "queued": "pending",
    "pending": "pending",
    "待处理": "pending",
    "未开始": "pending",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "finished": "completed",
    "已完成": "completed",
    "完成": "completed",
    "failed": "failed",
    "fail": "failed",
    "失败": "failed",
    "waiting": "waiting_on_customer",
    "waiting_on_customer": "waiting_on_customer",
    "waiting on customer": "waiting_on_customer",
    "待用户": "waiting_on_customer",
    "等待用户": "waiting_on_customer",
}


def _normalize_task_status_filter(status: str | None) -> str | None:
    text = str(status or "").strip()
    if not text:
        return None
    lowered = text.lower().replace("-", " ").replace("_", " ")
    return _TASK_STATUS_ALIASES.get(text.lower()) or _TASK_STATUS_ALIASES.get(lowered) or text


def _cache_key(entity_id: str, workspace_id: str) -> str:
    return f"{entity_id}:{workspace_id}"


def invalidate(workspace_id: str) -> None:
    """Drop cached summary so next chat rebuilds it."""
    _cache.pop(workspace_id, None)
    for key in [k for k in _cache if k.endswith(f":{workspace_id}")]:
        _cache.pop(key, None)


async def get_summary(db: AsyncSession, workspace_id: str, entity_id: str) -> str:
    """Return a short text summary of the workspace state.

    Cached for 5 minutes. Cheap DB queries (counts only, no joins).
    """
    key = _cache_key(entity_id, workspace_id)
    cached = _cache.get(key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    text = await _build_summary(db, workspace_id, entity_id)
    _cache[key] = (text, time.time())
    return text


async def _build_summary(db: AsyncSession, workspace_id: str, entity_id: str) -> str:
    """Build a concise workspace status block for the system prompt."""
    from packages.core.models.workspace import Workspace, AgentSubscription, Agent
    from packages.core.models.task import Task
    from packages.core.models.goal import Goal
    from packages.core.models.document import DocumentGroup

    parts: list[str] = []

    # Workspace basics
    ws = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not ws:
        return "Workspace not found."

    parts.append(f'Workspace: "{ws.name}" ({ws.kind or "project"})')
    if ws.primary_work:
        parts.append(f"Primary work: {ws.primary_work[:200]}")
    parts.append(
        "Freshness rule: this Workspace Context is newer than older chat/proposal "
        "history. Treat rejected/resolved proposal claims as historical only when "
        "they conflict with the live state below."
    )

    # Goal summary
    goals = (await db.execute(
        select(Goal.title, Goal.current_value, Goal.target_value, Goal.pace_status)
        .where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == "active",
        )
    )).all()
    if goals:
        goal_lines = []
        for g in goals:
            current = float(g.current_value or 0)
            target = float(g.target_value or 1)
            pct = min(100, int((current / target) * 100)) if target > 0 else 0
            pace = g.pace_status or "unknown"
            goal_lines.append(f"  - {g.title}: {pct}% ({pace})")
        parts.append(f"Goals ({len(goals)}):\n" + "\n".join(goal_lines))
    else:
        parts.append("Goals: none set")

    # Task counts by status
    task_counts = (await db.execute(
        select(Task.status, func.count().label("cnt"))
        .where(Task.entity_id == entity_id, Task.workspace_id == workspace_id)
        .group_by(Task.status)
    )).all()
    if task_counts:
        tc = {r.status: r.cnt for r in task_counts}
        total = sum(tc.values())
        status_parts = []
        for s in ["completed", "in_progress", "proposed", "pending", "failed", "waiting_on_customer"]:
            if tc.get(s):
                status_parts.append(f"{tc[s]} {s.replace('_', ' ')}")
        parts.append(f"Tasks ({total}): " + ", ".join(status_parts))
        active_tasks = (await db.execute(
            select(
                Task.id,
                Task.title,
                Task.status,
                Task.owner_service_key,
                Task.delegate_service_keys,
            )
            .where(
                Task.entity_id == entity_id,
                Task.workspace_id == workspace_id,
                Task.status.in_(["in_progress", "pending", "waiting_on_customer"]),
            )
            .order_by(Task.created_at.desc())
            .limit(5)
        )).all()
        if active_tasks:
            active_lines: list[str] = []
            for task in active_tasks:
                line = f"  - task_id={task.id} [{task.status}] {task.title}"
                if task.owner_service_key:
                    line += f" owner={task.owner_service_key}"
                delegates = list(task.delegate_service_keys or [])
                if delegates:
                    line += f" delegates={', '.join(map(str, delegates[:4]))}"
                active_lines.append(line)
            parts.append("Active tasks:\n" + "\n".join(active_lines))
    else:
        parts.append("Tasks: none")

    # Agents
    subs = (await db.execute(
        select(
            AgentSubscription.id,
            AgentSubscription.agent_id,
            AgentSubscription.service_key,
            AgentSubscription.name,
        )
        .where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).all()
    if subs:
        agent_ids = [s.agent_id for s in subs if s.agent_id]
        agents_by_id: dict[str, str] = {}
        if agent_ids:
            agents = (await db.execute(
                select(Agent.id, Agent.name).where(
                    Agent.id.in_(agent_ids),
                    Agent.deleted_at.is_(None),
                    or_(Agent.entity_id == entity_id, Agent.entity_id.is_(None)),
                )
            )).all()
            agents_by_id = {agent.id: agent.name for agent in agents if agent.id and agent.name}
        service_lines: list[str] = []
        for sub in subs[:8]:
            agent_name = agents_by_id.get(sub.agent_id) or sub.name or "Unknown"
            service_key = sub.service_key or "general"
            service_lines.append(
                f"  - service_key={service_key} agent=\"{agent_name}\" subscription_id={sub.id}"
            )
        if len(subs) > len(service_lines):
            service_lines.append("  - use workspace_search(category='agents') for the full list")
        parts.append(f"Agents/services ({len(subs)}):\n" + "\n".join(service_lines))
    else:
        parts.append("Agents: none assigned")

    # Knowledge base
    knowledge_groups = [
        group for group in (await db.execute(
            select(DocumentGroup)
            .where(DocumentGroup.entity_id == entity_id, DocumentGroup.workspace_id == workspace_id)
        )).scalars().all()
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    group_count = len(knowledge_groups)
    if group_count:
        knowledge_policy = (ws.operating_model or {}).get("knowledge") or {}
        mode = knowledge_policy.get("retrieval_mode") or "auto"
        auto = "auto-search on" if knowledge_policy.get("auto_search") is not False else "auto-search off"
        cite = "citations required" if knowledge_policy.get("citation_required") is not False else "citations optional"
        parts.append(f"Workspace Knowledge Nets: {group_count} ({mode}; {auto}; {cite})")
        if knowledge_policy.get("auto_search") is not False:
            parts.append(
                "Knowledge runtime: before answering or executing document-dependent requests, "
                "search workspace Knowledge Nets with rag(workspace_id=...) or a specific net via "
                "rag(net_ids=[...]), and cite source names."
            )
        if knowledge_policy.get("strict_mode") is True or mode == "strict":
            parts.append(
                "Knowledge strict mode: for knowledge-backed work, stay within attached "
                "workspace Knowledge Nets unless the user explicitly adds more sources."
            )

    try:
        from packages.core.services.workspace_readiness import list_configured_workspace_channels

        channels = await list_configured_workspace_channels(db, ws)
        if channels:
            channel_lines: list[str] = []
            for ch in channels[:8]:
                role = ch.get("role") or "channel"
                ch_type = ch.get("channel_type") or "unknown"
                linked = ch.get("linked_service_key") or "unassigned"
                built_in = "built-in" if ch.get("built_in") else "integration"
                channel_lines.append(f"  - {role}: {ch_type} ({built_in}; linked_service={linked})")
            parts.append("Configured channels:\n" + "\n".join(channel_lines))
            parts.append(
                "Channel source of truth: if webchat/internal_chat is listed above, "
                "do not say the workspace has no inbound channels. Do not recommend "
                "email/SMS/lead forms unless the user asks to add those channels."
            )
        else:
            parts.append("Configured channels: none")
    except Exception:
        logger.debug("Workspace channel summary failed", exc_info=True)

    op_rules = (ws.operating_model or {}).get("rules") or []
    if op_rules:
        parts.append(f"Operating rules: {len(op_rules)}")

    try:
        from packages.core.governance import get_policy
        policy = await get_policy(db, workspace_id)
        guardrails: list[str] = []
        if policy.never_allow_actions:
            guardrails.append(f"never allow {_compact_patterns(policy.never_allow_actions)}")
        if policy.hitl_required_actions:
            guardrails.append(f"approval required for {_compact_patterns(policy.hitl_required_actions)}")
        if policy.auto_approve_actions:
            guardrails.append(f"auto-approved exceptions {_compact_patterns(policy.auto_approve_actions)}")
        if policy.never_allow_capabilities:
            guardrails.append(f"never allow capabilities {_compact_patterns(policy.never_allow_capabilities)}")
        if policy.hitl_required_capabilities:
            guardrails.append(f"approval required for capabilities {_compact_patterns(policy.hitl_required_capabilities)}")
        if policy.auto_approve_capabilities:
            guardrails.append(f"auto-approved capability exceptions {_compact_patterns(policy.auto_approve_capabilities)}")
        if policy.max_risk_level != "high":
            guardrails.append(f"max risk {policy.max_risk_level}")
        if guardrails:
            parts.append("Runtime guardrails: " + "; ".join(guardrails) + ". These are checked before tools run.")
    except Exception:
        logger.debug("Workspace governance summary failed", exc_info=True)

    parts.append(
        "\nUse workspace_search to look up goals, tasks, agents, knowledge, artifacts/files, plans, rules, "
        "runtime evidence, or learning candidates. Use workspace_agent for persistent workspace changes "
        "such as tasks, rules, or knowledge bindings."
    )

    return "\n".join(parts)


# ── Dynamic search ────────────────────────────────────────────────────

async def workspace_search(
    db: AsyncSession,
    workspace_id: str,
    entity_id: str,
    *,
    query: str = "",
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 10,
    external_client: bool = False,
    public_agent_client: bool = False,
) -> str:
    """Search workspace data by category. Returns formatted text for the LLM."""
    q = (query or "").strip().lower()
    cat = (category or "all").strip().lower()
    status_filter = _normalize_task_status_filter(status)

    from packages.core.models.workspace import Workspace
    ws_exists = (await db.execute(
        select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not ws_exists:
        return "Workspace not found."

    results: list[str] = []

    if external_client:
        results.append(await _search_knowledge(
            db,
            workspace_id,
            entity_id,
            q,
            limit,
            client_visible_only=True,
            public_agent_visible_only=public_agent_client,
        ))
        text = "\n\n".join(r for r in results if r)
        return text or "No customer-visible workspace knowledge found."

    if cat in ("goals", "all"):
        results.append(await _search_goals(db, workspace_id, entity_id, q, limit))

    if cat in ("tasks", "all"):
        results.append(await _search_tasks(db, workspace_id, entity_id, q, status_filter, limit))

    if cat in ("agents", "all"):
        results.append(await _search_agents(db, workspace_id, entity_id, q, limit))

    if cat in ("knowledge", "all"):
        results.append(await _search_knowledge(db, workspace_id, entity_id, q, limit))

    if cat in ("artifacts", "files", "generated_files", "all"):
        results.append(await _search_artifacts(db, workspace_id, entity_id, q, limit))

    if cat in ("plans", "all"):
        results.append(await _search_plans(db, workspace_id, entity_id, q, limit))

    if cat in ("rules", "all"):
        results.append(await _search_rules(db, workspace_id, entity_id, q))

    if cat in ("history", "all"):
        results.append(await _search_history(db, workspace_id, entity_id, q, limit))

    if cat in ("runtime", "evidence", "learning", "all"):
        results.append(await _search_runtime_learning(db, workspace_id, entity_id, q, limit))

    text = "\n\n".join(r for r in results if r)
    return text or "No results found."


async def _search_goals(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.models.goal import Goal
    rows = (await db.execute(
        select(Goal).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == ws_id,
            Goal.status == "active",
        )
        .order_by(Goal.priority.asc()).limit(limit)
    )).scalars().all()
    if not rows:
        return ""
    lines = ["## Goals"]
    for g in rows:
        if q and q not in (g.title or "").lower() and q not in (g.metric_key or "").lower():
            continue
        current = float(g.current_value or 0)
        target = float(g.target_value or 1)
        pct = min(100, int((current / target) * 100)) if target > 0 else 0
        lines.append(
            f"- **{g.title}**: {current:,.0f} / {target:,.0f} ({pct}%) — {g.pace_status or 'unknown'}"
            + (f"\n  {g.description}" if g.description else "")
            + (f"\n  Cadence: {g.measurement_cadence}" if g.measurement_cadence else "")
            + (f" | Deadline: {g.deadline}" if g.deadline else "")
        )
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_tasks(
    db: AsyncSession, ws_id: str, entity_id: str, q: str, status_filter: str | None, limit: int,
) -> str:
    from packages.core.models.task import Task
    from packages.core.services.workspace_runtime import compact_runtime_json
    stmt = (
        select(Task)
        .where(Task.entity_id == entity_id, Task.workspace_id == ws_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return ""
    lines = ["## Tasks"]
    for t in rows:
        if q and q not in (t.title or "").lower() and q not in (t.description or "").lower():
            continue
        line = f"- task_id={t.id} [{t.status}] **{t.title}**"
        if t.owner_service_key:
            line += f" owner={t.owner_service_key}"
        delegates = list(t.delegate_service_keys or [])
        if delegates:
            line += f" delegates={', '.join(map(str, delegates[:4]))}"
        if t.description:
            line += f" — {t.description[:120]}"
        details = t.details if isinstance(t.details, dict) else {}
        runtime_context = details.get("runtime_context") or {}
        if runtime_context:
            line += f"\n  runtime_context: {compact_runtime_json(runtime_context, max_chars=500)}"
        if t.actual_output and isinstance(t.actual_output, dict):
            steps = t.actual_output.get("steps") or []
            done = sum(1 for s in steps if s.get("status") == "done")
            line += f" ({done}/{len(steps)} steps)"
            files = t.actual_output.get("files") or []
            if files:
                line += f" [{len(files)} files]"
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_agents(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.models.workspace import AgentSubscription, Agent
    subs = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == entity_id,
            AgentSubscription.workspace_id == ws_id,
            AgentSubscription.status == "active",
        ).limit(limit)
    )).scalars().all()
    if not subs:
        return ""
    agent_ids = [s.agent_id for s in subs if s.agent_id]
    agents_map: dict[str, Any] = {}
    if agent_ids:
        for a in (await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                Agent.deleted_at.is_(None),
                or_(Agent.entity_id == entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all():
            agents_map[a.id] = a
    lines = ["## Agents"]
    for s in subs:
        a = agents_map.get(s.agent_id) if s.agent_id else None
        name = a.name if a else "Unknown"
        if q and q not in name.lower() and q not in (s.service_key or "").lower():
            continue
        line = f"- **{name}** — service: {s.service_key or 'general'}"
        if a and a.system_prompt:
            line += f"\n  {a.system_prompt[:150]}"
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_knowledge(
    db: AsyncSession,
    ws_id: str,
    entity_id: str,
    q: str,
    limit: int,
    *,
    client_visible_only: bool = False,
    public_agent_visible_only: bool = False,
) -> str:
    from packages.core.models.document import DocumentGroup, Document, DocumentGroupMember
    from packages.core.models.workspace import Workspace
    from packages.core.services.document_access import (
        document_is_client_visible,
        document_is_public_agent_visible,
    )
    ws = (await db.execute(select(Workspace).where(
        Workspace.id == ws_id,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    ))).scalar_one_or_none()
    group_purposes = (
        (((ws.operating_model or {}).get("knowledge") or {}).get("group_purposes") or {})
        if ws
        else {}
    )
    groups = [
        group for group in (await db.execute(
            select(DocumentGroup).where(
                DocumentGroup.entity_id == entity_id,
                DocumentGroup.workspace_id == ws_id,
            ).limit(limit)
        )).scalars().all()
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if not groups:
        return ""
    lines = ["## Knowledge Base"]
    lines.append(
        "Use rag(workspace_id=...) for content-level lookup across these workspace Knowledge Nets, "
        "or rag(net_ids=[...]) for one net, and cite the document names you used."
    )
    for g in groups:
        group_matches = bool(q and q in (g.name or "").lower())
        purpose = group_purposes.get(g.id) or (((g.settings or {}).get("purpose")) or "")
        group_doc_stmt = (
            select(Document)
            .join(DocumentGroupMember, Document.id == DocumentGroupMember.document_id)
            .where(DocumentGroupMember.group_id == g.id)
            .where(Document.entity_id == entity_id, Document.is_trashed.is_(False))
            .order_by(Document.created_at.desc())
        )
        group_docs = list((await db.execute(group_doc_stmt)).scalars().all())
        if client_visible_only:
            group_docs = [
                doc for doc in group_docs
                if (
                    await document_is_public_agent_visible(
                        db,
                        doc,
                        entity_id=entity_id,
                        workspace_id=ws_id,
                    )
                    if public_agent_visible_only
                    else await document_is_client_visible(
                        db,
                        doc,
                        entity_id=entity_id,
                        workspace_id=ws_id,
                    )
                )
            ]
        doc_count = len(group_docs) if client_visible_only else (await db.execute(
            select(func.count()).select_from(DocumentGroupMember)
            .join(Document, Document.id == DocumentGroupMember.document_id)
            .where(DocumentGroupMember.group_id == g.id)
            .where(Document.entity_id == entity_id, Document.is_trashed.is_(False))
        )).scalar_one()
        if client_visible_only and doc_count == 0:
            continue
        doc_stmt = (
            select(Document)
            .join(DocumentGroupMember, Document.id == DocumentGroupMember.document_id)
            .where(DocumentGroupMember.group_id == g.id)
            .where(Document.entity_id == entity_id, Document.is_trashed.is_(False))
            .order_by(Document.created_at.desc())
            .limit(5)
        )
        if q and not group_matches:
            like = f"%{q}%"
            doc_stmt = doc_stmt.where(or_(
                Document.name.ilike(like),
                Document.file_type.ilike(like),
                Document.fs_path.ilike(like),
                Document.metadata_.cast(String).ilike(like),
            ))
        docs = list((await db.execute(doc_stmt)).scalars().all())
        if client_visible_only:
            docs = [
                doc for doc in docs
                if (
                    await document_is_public_agent_visible(
                        db,
                        doc,
                        entity_id=entity_id,
                        workspace_id=ws_id,
                    )
                    if public_agent_visible_only
                    else await document_is_client_visible(
                        db,
                        doc,
                        entity_id=entity_id,
                        workspace_id=ws_id,
                    )
                )
            ]
        if q and not group_matches and not docs:
            continue
        line = f"- **{g.name}** ({doc_count} docs)"
        if purpose:
            line += f" — {purpose[:120]}"
        lines.append(line)
        for doc in docs:
            status = f", vector={doc.vector_status}" if doc.vector_status else ""
            location = "" if client_visible_only else f", path={doc.fs_path}" if doc.fs_path else ""
            lines.append(f"  - {doc.name} ({doc.file_type or 'file'}{status}{location})")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_artifacts(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services.document_metadata import metadata_artifact, metadata_origin

    workspace_file_membership = (
        select(DocumentGroupMember.document_id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(
            DocumentGroupMember.document_id == Document.id,
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == ws_id,
            DocumentGroup.settings["workspace_file_bucket"].as_boolean().is_(True),
        )
        .exists()
    )
    workspace_origin = or_(
        workspace_file_membership,
        Document.metadata_["origin"]["workspace_id"].astext == ws_id,
    )
    stmt = (
        select(Document)
        .where(
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
            workspace_origin,
        )
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            Document.name.ilike(like),
            Document.file_type.ilike(like),
            Document.mime_type.ilike(like),
            Document.source.ilike(like),
            Document.fs_path.ilike(like),
            Document.file_url.ilike(like),
            Document.metadata_.cast(String).ilike(like),
        ))
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return ""

    lines = ["## Workspace Artifacts"]
    for doc in rows:
        meta = doc.metadata_ or {}
        origin = metadata_origin(meta)
        artifact = metadata_artifact(meta)
        role = artifact.get("role") or "artifact"
        task_id = origin.get("task_id")
        tool = origin.get("tool_name")
        line = (
            f"- document_id={doc.id} **{doc.name}** "
            f"({doc.file_type or doc.mime_type or 'file'}, source={doc.source}, role={role})"
        )
        if task_id:
            line += f" task_id={task_id}"
        if tool:
            line += f" tool={tool}"
        if doc.fs_path:
            line += f"\n  fs_path={doc.fs_path}"
        if doc.file_url:
            line += f"\n  file_url={doc.file_url}"
        lines.append(line)
    return "\n".join(lines)


async def _search_plans(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    plans = (await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.workspace_id == ws_id,
            ExecutionPlan.status.in_(["running", "completed", "failed"]),
        ).order_by(ExecutionPlan.created_at.desc()).limit(limit)
    )).scalars().all()
    if not plans:
        return ""
    lines = ["## Execution Plans"]
    for p in plans:
        steps = (await db.execute(
            select(ExecutionStep.step_key, ExecutionStep.step_status)
            .where(ExecutionStep.plan_id == p.id)
        )).all()
        done = sum(1 for s in steps if s.step_status == "done")
        failed = sum(1 for s in steps if s.step_status == "failed")
        task_ref = f" task_id={p.task_id}" if p.task_id else ""
        lines.append(f"- Plan {p.id[:8]}… [{p.status}]{task_ref} — {done}/{len(steps)} done, {failed} failed")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_rules(db: AsyncSession, ws_id: str, entity_id: str, q: str) -> str:
    from packages.core.models.workspace import Workspace
    from packages.core.governance import get_policy
    ws = (await db.execute(select(Workspace).where(
        Workspace.id == ws_id,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if not ws:
        return ""
    rules = (ws.operating_model or {}).get("rules") or []
    lines = ["## Rules"]
    for r in rules:
        desc = r.get("description", "")
        if q and q not in desc.lower() and q not in (r.get("rule_key") or "").lower():
            continue
        lines.append(f"- [{r.get('severity', '?')}] {desc[:200]}")
    policy = await get_policy(db, ws_id)
    policy_lines: list[str] = []
    if policy.never_allow_actions:
        policy_lines.append("Never allow: " + ", ".join(policy.never_allow_actions))
    if policy.hitl_required_actions:
        policy_lines.append("Approval required: " + ", ".join(policy.hitl_required_actions))
    if policy.auto_approve_actions:
        policy_lines.append("Auto-approved exceptions: " + ", ".join(policy.auto_approve_actions))
    if policy.never_allow_capabilities:
        policy_lines.append("Never allow capabilities: " + ", ".join(policy.never_allow_capabilities))
    if policy.hitl_required_capabilities:
        policy_lines.append("Approval required capabilities: " + ", ".join(policy.hitl_required_capabilities))
    if policy.auto_approve_capabilities:
        policy_lines.append("Auto-approved capability exceptions: " + ", ".join(policy.auto_approve_capabilities))
    if policy.max_risk_level != "high":
        policy_lines.append(f"Max risk level: {policy.max_risk_level}")
    if policy_lines:
        lines.append("Runtime guardrails:")
        lines.extend(f"- {line}" for line in policy_lines)
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_history(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.models.task import Message, Conversation
    conv = (await db.execute(
        select(Conversation.id).where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == ws_id,
            Conversation.scope == "workspace_main",
        ).limit(1)
    )).scalar_one_or_none()
    if not conv:
        return ""
    msgs = (await db.execute(
        select(Message).where(
            Message.conversation_id == conv,
            Message.pending_action.isnot(None),
            Message.pending_action["kind"].as_string().isnot(None),
        ).order_by(Message.created_at.desc()).limit(limit)
    )).scalars().all()
    if not msgs:
        return ""
    lines = ["## Recent Decisions"]
    for m in msgs:
        action = m.pending_action or {}
        resolved = "resolved" if m.resolved_at else "pending"
        choice = (m.resolution or {}).get("choice", "")
        lines.append(f"- [{resolved}] {action.get('kind', '?')}: {choice} — {(m.content or '')[:100]}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _search_runtime_learning(db: AsyncSession, ws_id: str, entity_id: str, q: str, limit: int) -> str:
    from packages.core.services.runtime_learning import (
        format_runtime_learning_context,
        list_learning_candidates,
        list_runtime_evidence,
    )

    candidates = await list_learning_candidates(
        db,
        entity_id=entity_id,
        workspace_id=ws_id,
        status=None,
        limit=limit,
    )
    evidence = await list_runtime_evidence(
        db,
        entity_id=entity_id,
        workspace_id=ws_id,
        limit=limit,
    )
    if q:
        candidates = [
            c for c in candidates
            if q in (c.title or "").lower()
            or q in (c.summary or "").lower()
            or q in str(c.payload or {}).lower()
        ]
        evidence = [
            ev for ev in evidence
            if q in (ev.summary or "").lower()
            or q in str(ev.details or {}).lower()
            or q in str(ev.metrics or {}).lower()
        ]
    return format_runtime_learning_context(
        evidence=evidence,
        candidates=candidates,
        max_items=limit,
    )
