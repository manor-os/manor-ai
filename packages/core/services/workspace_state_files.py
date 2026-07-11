"""Generated workspace state and file-index Markdown caches.

The canonical memory docs are the fast path for agents that need to know
"what is true in this workspace right now" without scanning every task,
document, and artifact row. These files are cache outputs, not durable user
notes: source-of-truth data stays in DB rows and user-visible files.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.memory.canonical import (
    ensure_workspace_memory_docs,
    write_workspace_memory_file,
)
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.execution import ExecutionPlan
from packages.core.models.goal import Goal
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceWorkBatch
from packages.core.services.document_metadata import metadata_artifact, metadata_origin


STATE_FILENAME = "STATE.md"
FILES_FILENAME = "FILES.md"


@dataclass
class WorkspaceFileEntry:
    key: str
    name: str
    description: str
    location: str
    kind: str = ""
    document_id: str = ""
    group: str = ""
    source: str = ""
    task_id: str = ""
    task_title: str = ""
    step_key: str = ""
    updated_at: datetime | None = None


async def refresh_workspace_state_files(
    db: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
    max_files: int = 120,
) -> dict[str, Any]:
    """Refresh STATE.md and FILES.md for a workspace.

    Returns a small summary for callers that want to log/cache observability.
    """
    now = now or datetime.now(timezone.utc)
    ensure_workspace_memory_docs(
        workspace.entity_id,
        workspace.id,
        workspace_name=workspace.name,
        workspace_kind=workspace.kind,
    )

    goals = await _active_goals(db, workspace)
    subscriptions = await _subscriptions(db, workspace)
    recent_tasks = await _recent_tasks(db, workspace, now=now)
    recent_plans = await _recent_plans(db, workspace)
    active_batches = await _active_batches(db, workspace)
    files = await _workspace_files(db, workspace, recent_tasks=recent_tasks, max_files=max_files)

    state_md = _render_state_md(
        workspace,
        now=now,
        goals=goals,
        subscriptions=subscriptions,
        recent_tasks=recent_tasks,
        recent_plans=recent_plans,
        active_batches=active_batches,
        file_count=len(files),
    )
    files_md = _render_files_md(workspace, now=now, files=files)

    write_workspace_memory_file(workspace.entity_id, workspace.id, STATE_FILENAME, state_md)
    write_workspace_memory_file(workspace.entity_id, workspace.id, FILES_FILENAME, files_md)
    return {
        "workspace_id": workspace.id,
        "state_file": STATE_FILENAME,
        "files_file": FILES_FILENAME,
        "file_count": len(files),
        "goal_count": len(goals),
        "recent_task_count": len(recent_tasks),
    }


async def _active_goals(db: AsyncSession, workspace: Workspace) -> list[Goal]:
    return list((await db.execute(
        select(Goal).where(
            Goal.entity_id == workspace.entity_id,
            Goal.workspace_id == workspace.id,
            Goal.status == "active",
        ).order_by(Goal.priority.desc(), Goal.created_at.desc()).limit(20)
    )).scalars().all())


async def _subscriptions(
    db: AsyncSession, workspace: Workspace,
) -> list[tuple[AgentSubscription, Agent | None]]:
    subs = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.status == "active",
        ).order_by(AgentSubscription.created_at.asc())
    )).scalars().all())
    if not subs:
        return []
    agent_ids = [sub.agent_id for sub in subs if sub.agent_id]
    agents = list((await db.execute(
        select(Agent).where(Agent.id.in_(agent_ids))
    )).scalars().all()) if agent_ids else []
    by_id = {agent.id: agent for agent in agents}
    return [(sub, by_id.get(sub.agent_id)) for sub in subs]


async def _recent_tasks(
    db: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime,
    limit: int = 40,
) -> list[Task]:
    cutoff = now - timedelta(days=30)
    return list((await db.execute(
        select(Task).where(
            Task.entity_id == workspace.entity_id,
            Task.workspace_id == workspace.id,
            Task.created_at >= cutoff,
        ).order_by(desc(Task.updated_at)).limit(limit)
    )).scalars().all())


async def _recent_plans(
    db: AsyncSession, workspace: Workspace, *, limit: int = 12,
) -> list[ExecutionPlan]:
    return list((await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.entity_id == workspace.entity_id,
            ExecutionPlan.workspace_id == workspace.id,
        ).order_by(desc(ExecutionPlan.updated_at)).limit(limit)
    )).scalars().all())


async def _active_batches(
    db: AsyncSession, workspace: Workspace, *, limit: int = 8,
) -> list[WorkspaceWorkBatch]:
    return list((await db.execute(
        select(WorkspaceWorkBatch).where(
            WorkspaceWorkBatch.entity_id == workspace.entity_id,
            WorkspaceWorkBatch.workspace_id == workspace.id,
            WorkspaceWorkBatch.status == "active",
        ).order_by(desc(WorkspaceWorkBatch.updated_at)).limit(limit)
    )).scalars().all())


async def _workspace_files(
    db: AsyncSession,
    workspace: Workspace,
    *,
    recent_tasks: list[Task],
    max_files: int,
) -> list[WorkspaceFileEntry]:
    entries: dict[str, WorkspaceFileEntry] = {}

    for entry in _task_artifact_entries(recent_tasks):
        _merge_entry(entries, entry)

    group_rows = list((await db.execute(
        select(Document, DocumentGroup.name)
        .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(
            Document.entity_id == workspace.entity_id,
            Document.is_trashed == False,  # noqa: E712
            DocumentGroup.entity_id == workspace.entity_id,
            DocumentGroup.workspace_id == workspace.id,
        )
        .order_by(desc(Document.updated_at))
        .limit(max_files)
    )).all())
    for doc, group_name in group_rows:
        _merge_entry(entries, _document_entry(doc, group=str(group_name or "")))

    origin_docs = list((await db.execute(
        select(Document).where(
            Document.entity_id == workspace.entity_id,
            Document.is_trashed == False,  # noqa: E712
            Document.metadata_["origin"]["workspace_id"].astext == workspace.id,
        ).order_by(desc(Document.updated_at)).limit(max_files)
    )).scalars().all())
    for doc in origin_docs:
        _merge_entry(entries, _document_entry(doc))

    return sorted(
        entries.values(),
        key=lambda item: item.updated_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:max_files]


def _task_artifact_entries(tasks: list[Task]) -> list[WorkspaceFileEntry]:
    out: list[WorkspaceFileEntry] = []
    for task in tasks:
        actual = task.actual_output if isinstance(task.actual_output, dict) else {}
        step_summaries = {
            str(step.get("key") or ""): str(step.get("result_summary") or "").strip()
            for step in actual.get("steps") or []
            if isinstance(step, dict)
        }
        for file in _collect_output_files(actual):
            location = _file_location(file)
            document_id = str(file.get("document_id") or file.get("doc_id") or "").strip()
            if not location and not document_id:
                continue
            step_key = str(file.get("step") or file.get("step_key") or file.get("key") or "").strip()
            description = (
                _first_text(file, ("description", "summary", "caption", "alt", "purpose"))
                or step_summaries.get(step_key)
                or f"Generated by task: {task.title}"
            )
            out.append(WorkspaceFileEntry(
                key=document_id or location,
                name=_file_name(file, location=location, fallback="Generated file"),
                description=_compact(description, 180),
                location=location or f"document:{document_id}",
                kind=str(file.get("type") or file.get("kind") or file.get("mime_type") or "").strip(),
                document_id=document_id,
                source="task_output",
                task_id=task.id,
                task_title=task.title,
                step_key=step_key,
                updated_at=task.completed_at or task.updated_at or task.created_at,
            ))
    return out


def _document_entry(doc: Document, *, group: str = "") -> WorkspaceFileEntry:
    origin = metadata_origin(doc.metadata_)
    artifact = metadata_artifact(doc.metadata_)
    location = str(doc.fs_path or doc.file_url or f"document:{doc.id}")
    source_bits = [str(doc.source or "document")]
    if artifact.get("role"):
        source_bits.append(f"role={artifact['role']}")
    description = _document_description(doc, group=group, origin=origin, artifact=artifact)
    return WorkspaceFileEntry(
        key=doc.id or location,
        name=str(doc.name or _basename(location) or "Document"),
        description=description,
        location=location,
        kind=str(doc.file_type or doc.mime_type or ""),
        document_id=doc.id,
        group=group,
        source=", ".join(source_bits),
        task_id=str(origin.get("task_id") or ""),
        step_key=str(origin.get("tool_name") or ""),
        updated_at=doc.updated_at or doc.created_at,
    )


def _document_description(
    doc: Document,
    *,
    group: str,
    origin: dict[str, Any],
    artifact: dict[str, Any],
) -> str:
    parts: list[str] = []
    if group:
        parts.append(f"Knowledge group: {group}")
    if artifact.get("role"):
        parts.append(f"Artifact role: {artifact['role']}")
    if origin.get("task_id"):
        parts.append(f"Produced or used by task {origin['task_id']}")
    if not parts:
        parts.append(str(doc.source or "Workspace document"))
    return _compact("; ".join(parts), 180)


def _merge_entry(entries: dict[str, WorkspaceFileEntry], entry: WorkspaceFileEntry) -> None:
    key = entry.document_id or entry.location or entry.key
    key = key.strip()
    if not key:
        return
    current = entries.get(key)
    if current is None:
        entries[key] = entry
        return

    current.name = current.name or entry.name
    current.description = _prefer_detail(current.description, entry.description)
    current.location = current.location or entry.location
    current.kind = current.kind or entry.kind
    current.document_id = current.document_id or entry.document_id
    current.group = current.group or entry.group
    current.source = _join_unique(current.source, entry.source)
    current.task_id = current.task_id or entry.task_id
    current.task_title = current.task_title or entry.task_title
    current.step_key = current.step_key or entry.step_key
    if entry.updated_at and (current.updated_at is None or entry.updated_at > current.updated_at):
        current.updated_at = entry.updated_at


def _collect_output_files(actual_output: dict[str, Any]) -> list[dict[str, Any]]:
    raw_files: list[Any] = []
    if isinstance(actual_output.get("files"), list):
        raw_files.extend(actual_output["files"])
    for step in actual_output.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("files") or []:
            if isinstance(item, dict) and step.get("key") and not item.get("step"):
                raw_files.append({**item, "step": step.get("key")})
            else:
                raw_files.append(item)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw_files):
        if not isinstance(item, dict):
            continue
        location = _file_location(item)
        doc_id = str(item.get("document_id") or item.get("doc_id") or "").strip()
        key = doc_id or location or f"artifact:{idx}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _render_state_md(
    workspace: Workspace,
    *,
    now: datetime,
    goals: list[Goal],
    subscriptions: list[tuple[AgentSubscription, Agent | None]],
    recent_tasks: list[Task],
    recent_plans: list[ExecutionPlan],
    active_batches: list[WorkspaceWorkBatch],
    file_count: int,
) -> str:
    lines = [
        "# Workspace State Cache",
        "",
        "<!-- manor-generated: workspace-state-cache -->",
        "",
        "This file is generated from Manor runtime state. Do not store durable",
        "operator guidance here; use WORKSPACE.md, RULES.md, MEMORY.md, or",
        "LEARNINGS.md for human-authored operating memory.",
        "",
        f"- Refreshed at: {now.isoformat()}",
        f"- Workspace: {workspace.name} (`{workspace.id}`)",
        f"- Status: {workspace.status}",
        f"- Kind: {workspace.kind or 'unspecified'}",
        f"- Cached files in FILES.md: {file_count}",
        "",
        "## Goals",
    ]
    if goals:
        lines.extend(["| Goal | Progress | Pace | Deadline |", "| --- | --- | --- | --- |"])
        for goal in goals:
            cur = _number(goal.current_value)
            tgt = _number(goal.target_value)
            progress = f"{cur} / {tgt}" if cur or tgt else "unknown"
            deadline = goal.deadline.isoformat() if goal.deadline else "none"
            lines.append(
                f"| {_cell(goal.title)} | {_cell(progress)} | "
                f"{_cell(goal.pace_status or 'unknown')} | {_cell(deadline)} |"
            )
    else:
        lines.append("_No active workspace goals._")

    lines.extend(["", "## Agents And Services"])
    if subscriptions:
        lines.extend(["| Service | Agent | Status |", "| --- | --- | --- |"])
        for sub, agent in subscriptions:
            agent_name = agent.name if agent else sub.agent_id
            lines.append(
                f"| {_cell(sub.service_key or 'unassigned')} | "
                f"{_cell(agent_name or 'unknown')} | {_cell(sub.status)} |"
            )
    else:
        lines.append("_No active agent subscriptions._")

    lines.extend(["", "## Current Work"])
    if active_batches:
        for batch in active_batches:
            lines.append(
                f"- Batch `{batch.id}` status={batch.status}, "
                f"tasks={len(batch.task_ids or [])}, source={batch.source_kind or 'unknown'}"
            )
    else:
        lines.append("_No active work batch._")

    lines.extend(["", "## Recent Tasks"])
    if recent_tasks:
        for task in recent_tasks[:18]:
            file_count_for_task = len(_collect_output_files(task.actual_output or {})) if isinstance(task.actual_output, dict) else 0
            files = f", files={file_count_for_task}" if file_count_for_task else ""
            lines.append(
                f"- [{task.status}] `{task.id}` {task.title} "
                f"(owner={task.owner_service_key or 'unassigned'}{files})"
            )
    else:
        lines.append("_No recent tasks._")

    lines.extend(["", "## Recent Plans"])
    if recent_plans:
        for plan in recent_plans[:12]:
            lines.append(
                f"- `{plan.id}` status={plan.status}, task={plan.task_id or 'none'}, "
                f"mode={plan.execution_mode}"
            )
    else:
        lines.append("_No recent plans._")

    lines.extend([
        "",
        "## Cache Notes",
        "- FILES.md is the workspace file wiki: use it first when looking for generated artifacts or workspace documents.",
        "- STATE.md is refreshed before Strategist review and after task finalization.",
        "- Database rows remain the source of truth when this cache looks stale.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _render_files_md(
    workspace: Workspace,
    *,
    now: datetime,
    files: list[WorkspaceFileEntry],
) -> str:
    lines = [
        "# Workspace Files Wiki",
        "",
        "<!-- manor-generated: workspace-files-cache -->",
        "",
        "This is the generated file index for the workspace. It is a quick lookup",
        "cache for agents and operators: what exists, what it is, and where to find",
        "it. Do not paste large file contents here; keep source material in",
        "Knowledge or the referenced file location.",
        "",
        f"- Refreshed at: {now.isoformat()}",
        f"- Workspace: {workspace.name} (`{workspace.id}`)",
        f"- Files indexed: {len(files)}",
        "",
        "## File Index",
    ]
    if not files:
        lines.append("_No workspace files or generated artifacts are indexed yet._")
        return "\n".join(lines).rstrip() + "\n"

    lines.extend([
        "| Name | Document ID | What | Location | Origin | Updated |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    for item in files:
        origin_bits = []
        if item.group:
            origin_bits.append(f"group={item.group}")
        if item.task_title:
            origin_bits.append(f"task={item.task_title}")
        elif item.task_id:
            origin_bits.append(f"task={item.task_id}")
        if item.source:
            origin_bits.append(item.source)
        updated = item.updated_at.isoformat() if item.updated_at else "unknown"
        lines.append(
            f"| {_cell(item.name)} | {_cell(item.document_id or '-')} | "
            f"{_cell(item.description or item.kind or 'Workspace file')} | "
            f"{_cell(item.location)} | {_cell('; '.join(origin_bits) or 'workspace')} | "
            f"{_cell(updated)} |"
        )

    lines.extend([
        "",
        "## Lookup Rules",
        "- Prefer `document_id` when present for Knowledge/document APIs.",
        "- Prefer `fs_path` or relative locations for filesystem tools.",
        "- If an expected output is missing here, inspect the source task output before regenerating it.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _file_location(file: dict[str, Any]) -> str:
    for key in (
        "fs_path",
        "saved_to",
        "path",
        "file_path",
        "output_path",
        "file_url",
        "document_url",
        "image_url",
        "video_url",
        "result_url",
        "output_url",
        "url",
    ):
        value = str(file.get(key) or "").strip()
        if value:
            return value
    return ""


def _file_name(file: dict[str, Any], *, location: str, fallback: str) -> str:
    value = _first_text(file, ("name", "filename", "original_name", "title"))
    if value:
        return value
    base = _basename(location)
    return base or fallback


def _first_text(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _basename(location: str) -> str:
    text = str(location or "").strip().rstrip("/")
    if not text:
        return ""
    return PurePosixPath(text.replace("\\", "/")).name


def _compact(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _cell(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|")


def _number(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _prefer_detail(left: str, right: str) -> str:
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left:
        return right
    if not right:
        return left
    return right if len(right) > len(left) else left


def _join_unique(left: str, right: str) -> str:
    values: list[str] = []
    for raw in (left, right):
        for part in str(raw or "").split(","):
            text = part.strip()
            if text and text not in values:
                values.append(text)
    return ", ".join(values)
