"""Workspace runtime schedule wiring and agent runtime resolution.

This module owns both meanings of "workspace runtime":
  * scheduler rows that make a workspace autonomous
  * per-turn agent scope for chat, tasks, channels, and workers
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.task import TaskLogType, TaskType
from packages.core.ai.runtime import (
    ChatSurface,
    RuntimeMCPProviderToolScope,
    RuntimeMCPProviderToolScopeFactory,
    RuntimeProfile,
    runtime_agent_tool_scope,
    runtime_workspace_turn_profile_names,
)
from packages.core.ai.runtime.capability_bindings import (
    expand_runtime_capability_binding,
    runtime_binding_owner_matches,
)
from packages.core.ai.runtime.task_requirements import (
    task_runtime_capabilities_from_context,
    task_runtime_capability_tools,
)
from packages.core.ai.runtime.tool_visibility import (
    WORKSPACE_LEDGER_VISUALIZATION_TOOL,
    runtime_expand_workspace_ledger_tools,
)
from packages.core.constants.agents import is_master_agent
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    workspace_autonomy_state,
)
from packages.core.services.ledger_query_service import (
    workspace_ledger_runtime_tools,
    workspace_queryable_ledger_configs,
)
from packages.core.services.workspace_access import user_can_write_workspace_artifacts

logger = logging.getLogger(__name__)

WORKSPACE_CHAT_SCOPES = {"workspace_main", "workspace_thread"}


def _settings_declare_ledger(value: object) -> bool:
    """Detect an installed ledger contract with a registered query adapter."""

    return bool(workspace_queryable_ledger_configs(value))


def is_workspace_chat_conversation(conv: Conversation) -> bool:
    """Return true only for first-party Workspace Chat conversations."""
    return bool(conv.workspace_id) and (conv.scope or "") in WORKSPACE_CHAT_SCOPES


@dataclass
class WorkspaceRuntimeEnvelope:
    """Resolved runtime scope for one agent turn."""

    workspace_id: str | None = None
    task_id: str | None = None
    thread_ref_kind: str | None = None
    thread_ref_id: str | None = None
    # Manor-level RuntimeProfile and the separate tool-visibility profile.
    runtime_profile: str | None = None
    tool_profile: str | None = None
    extra_context: str | None = None
    is_master: bool = False
    bound_tool_names: set[str] | None = None
    mcp_allowed_names: set[str] | None = None
    mcp_provider_scopes: tuple[RuntimeMCPProviderToolScope, ...] = ()
    mcp_scope_unrestricted: bool = False
    capability_ids: set[str] = field(default_factory=set)
    service_agent_ids: list[str] = field(default_factory=list)

def compact_runtime_json(value, *, max_chars: int = 1400) -> str:
    if not value:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated {len(text) - max_chars} chars]"


def _task_extra_context(task: Task, *, workspace_scoped: bool | None = None) -> str:
    details = task.details or {}
    runtime_context = details.get("runtime_context") or {}
    is_workspace_task = bool(task.workspace_id) if workspace_scoped is None else bool(workspace_scoped)
    lines = [
        "## Active Workspace Task Thread" if is_workspace_task else "## Active Task Thread",
        f"- task_id: {task.id}",
        f"- title: {task.title}",
        f"- status: {task.status}",
        f"- priority: {task.priority}",
        f"- task_type: {task.task_type}",
    ]
    if task.description:
        description_limit = (
            4000 if task.task_type == TaskType.INTERACTIVE.value else 500
        )
        lines.append(f"- description: {str(task.description)[:description_limit]}")
    if runtime_context:
        lines.append("- runtime_context: " + compact_runtime_json(runtime_context))
    if task.task_type == TaskType.INTERACTIVE.value:
        session_config = details.get("session")
        lines.extend([
            "## Interactive Task Session",
            "The conversation is the task's execution surface. Conduct the "
            "session as the assigned Host Agent: pursue the Task objective, "
            "adapt the dialogue structure to the user and domain, teach or "
            "assess when the Task calls for it, and make progress visible in "
            "the conversation. Do not turn it into a background job unless "
            "the user explicitly creates separate work.",
        ])
        if isinstance(session_config, dict) and session_config:
            lines.append(
                "- session_config: "
                + compact_runtime_json(session_config, max_chars=3000)
            )
    if is_workspace_task:
        lines.append(
            "When the latest user message adds requirements, constraints, "
            "knowledge refs, goals, or rules for this task, persist them with "
            "`workspace_update_task_runtime` or `workspace_agent` "
            "action='update_task_runtime' before acting. If it is only an FYI, "
            "acknowledge it and avoid changing runtime requirements."
        )
    else:
        lines.append(
            "This is an independent task, not a workspace-governed task. Use "
            "ordinary task tools for task updates; do not call workspace-only "
            "runtime tools unless a workspace_id is explicitly attached later."
        )
    return "\n".join(lines)


def _pending_hitl_id(pending_action: dict) -> str:
    operation = pending_action.get("operation") if isinstance(pending_action.get("operation"), dict) else {}
    return str(
        pending_action.get("draft_id")
        or pending_action.get("approval_token")
        or pending_action.get("review_id")
        or pending_action.get("step_id")
        or operation.get("draft_id")
        or ""
    ).strip()


def _pending_hitl_context_row(message: Message) -> dict:
    pending_action = message.pending_action if isinstance(message.pending_action, dict) else {}
    operation = pending_action.get("operation") if isinstance(pending_action.get("operation"), dict) else {}
    patches = operation.get("patches") or pending_action.get("patches") or []
    patch_summaries: list[dict] = []
    if isinstance(patches, list):
        for patch in patches[:5]:
            if not isinstance(patch, dict):
                continue
            payload = patch.get("payload")
            patch_summaries.append({
                "op": patch.get("op"),
                "path": patch.get("path"),
                "payload_keys": sorted(payload.keys())[:8] if isinstance(payload, dict) else None,
            })
    row = {
        "message_id": message.id,
        "kind": pending_action.get("kind"),
        "hitl_id": _pending_hitl_id(pending_action) or None,
        "prompt": pending_action.get("prompt") or pending_action.get("title") or pending_action.get("summary"),
        "action": pending_action.get("action"),
        "tool": pending_action.get("tool"),
        "options": pending_action.get("options"),
        "assistant_message": str(message.content or "")[:500],
    }
    if operation:
        row["operation"] = {
            "kind": operation.get("kind"),
            "draft_id": operation.get("draft_id"),
            "summary": operation.get("summary") or operation.get("title") or operation.get("description"),
            "patches": patch_summaries,
        }
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


async def _pending_hitl_extra_context(
    db: AsyncSession | None,
    *,
    conversation_id: str | None,
) -> str | None:
    if not db or not conversation_id:
        return None
    rows = list((await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.pending_action.isnot(None),
            Message.resolved_at.is_(None),
        )
        .order_by(Message.created_at.desc())
        .limit(10)
    )).scalars().all())
    items = [
        _pending_hitl_context_row(row)
        for row in rows
        if isinstance(row.pending_action, dict) and row.pending_action.get("kind")
    ]
    if not items:
        return None
    return "\n".join([
        "## Open Workspace HITL Requests",
        "These unresolved workspace chat pending actions are part of the conversation state.",
        "When the latest user message is semantically answering one of them, call `workspace_resolve_hitl` with the matching `message_id` or `hitl_id` and the intended action. If the message is not answering HITL, continue normally and do not resolve anything.",
        compact_runtime_json(items, max_chars=5000),
    ])


async def _open_task_blockers_extra_context(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    workspace_id: str | None,
) -> str | None:
    """Render open lease-origin HITL requests (paused task steps) so the
    chat agent knows a task is waiting on the user — and routes an answer
    back to it instead of starting a fresh delegation without the task's
    tools. Complements ``_pending_hitl_extra_context``: that section covers
    chat-message pending actions; this one covers the ``hitl_requests``
    table rows minted by ``lease_needs_human``."""
    if not db or not entity_id or not workspace_id:
        return None
    try:
        from packages.core.services.task_blockers import list_open_task_blockers

        blockers = await list_open_task_blockers(
            db, entity_id=entity_id, workspace_id=workspace_id,
        )
    except Exception:
        logger.warning("open task blocker context failed (ignored)", exc_info=True)
        return None
    if not blockers:
        return None
    return "\n".join([
        "## Open Task Blockers",
        "Paused task steps waiting on the user. When the latest user message "
        "answers, confirms, or declines one of them, call `answer_task_blocker` "
        "with its `request_id` — the original task then resumes with its own "
        "tools. Do not start a new delegation for the same goal while its "
        "blocker is open. Login-wall blockers (`needs_login`) can only be "
        "completed from the card's sign-in flow, not by this tool.",
        compact_runtime_json(blockers, max_chars=4000),
    ])


async def _load_task_for_runtime(
    db: AsyncSession,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    task_id: str,
) -> Task | None:
    filters = [Task.id == task_id]
    if entity_id:
        filters.append(Task.entity_id == entity_id)
    if workspace_id:
        filters.append(Task.workspace_id == workspace_id)
    return (await db.execute(select(Task).where(*filters))).scalar_one_or_none()


async def load_conversation_runtime_context(
    db: AsyncSession | None,
    *,
    conversation_id: str | None,
    entity_id: str | None,
) -> dict:
    """Resolve workspace/task metadata from a conversation id."""
    if not db or not conversation_id:
        return {}

    conv_filters = [Conversation.id == conversation_id]
    if entity_id:
        conv_filters.append(Conversation.entity_id == entity_id)
    conv = (await db.execute(
        select(Conversation).where(*conv_filters)
    )).scalar_one_or_none()
    if not conv:
        raise LookupError("Conversation not found")

    workspace_scoped = is_workspace_chat_conversation(conv)
    runtime: dict = {
        "workspace_id": conv.workspace_id if workspace_scoped else None,
        "thread_ref_kind": conv.thread_ref_kind if workspace_scoped else None,
        "thread_ref_id": conv.thread_ref_id if workspace_scoped else None,
        "agent_subscription_id": (
            conv.agent_subscription_id if workspace_scoped else None
        ),
    }

    task = None
    if workspace_scoped and conv.thread_ref_kind == "task" and conv.thread_ref_id:
        task = await _load_task_for_runtime(
            db,
            entity_id=entity_id,
            workspace_id=conv.workspace_id,
            task_id=conv.thread_ref_id,
        )

    if task:
        runtime["task_id"] = task.id
        runtime["extra_context"] = _task_extra_context(task)

    return runtime


async def _resolve_agent_tool_scope(
    db: AsyncSession | None,
    *,
    agent_id: str | None,
    is_master: bool,
) -> tuple[
    set[str] | None,
    set[str] | None,
    tuple[RuntimeMCPProviderToolScope, ...],
]:
    """Resolve first-party and MCP tool scope for a non-master agent.

    ``None`` preserves the existing master semantics in ``ToolPool``. Custom
    agents get explicit sets so discovery stays scoped by their bindings.
    """
    scope = await runtime_agent_tool_scope(
        db,
        agent_id=agent_id,
        is_master=is_master,
    )
    bound_tool_names, mcp_allowed_names = scope.mutable_pair()
    return (
        bound_tool_names,
        mcp_allowed_names,
        tuple(getattr(scope, "mcp_provider_scopes", ()) or ()),
    )


async def _agent_has_visible_skills(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    agent_id: str | None,
) -> bool:
    """Return whether the agent can run at least one runtime-visible skill."""

    if not db or not entity_id or not agent_id:
        return False
    from packages.core.models.skill import AgentSkillBinding, Skill

    bound_skill_ids = (
        select(AgentSkillBinding.skill_id)
        .where(
            AgentSkillBinding.agent_id == agent_id,
            AgentSkillBinding.status == "active",
        )
        .scalar_subquery()
    )
    visible_skill = (await db.execute(
        select(Skill.id)
        .where(
            Skill.status == "active",
            or_(
                Skill.entity_id.is_(None),
                and_(Skill.entity_id == entity_id, Skill.is_public.is_(True)),
                and_(Skill.entity_id == entity_id, Skill.id.in_(bound_skill_ids)),
            ),
        )
        .limit(1)
    )).scalar_one_or_none()
    return bool(visible_skill)


async def agent_has_visible_skills(
    db: AsyncSession | None,
    *,
    entity_id: str | None,
    agent_id: str | None,
) -> bool:
    """Public narrow resolver shared by runtime assembly and authorization."""

    return await _agent_has_visible_skills(
        db,
        entity_id=entity_id,
        agent_id=agent_id,
    )


def _task_service_keys(task: Task) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    raw_keys = [task.owner_service_key, *(task.delegate_service_keys or [])]
    for raw in raw_keys:
        key = str(raw or "").strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _task_runtime_capability_scope(task: Task | None) -> tuple[set[str], set[str]]:
    if task is None:
        return set(), set()
    runtime_context = _as_dict((task.details or {}).get("runtime_context"))
    capability_ids = set(task_runtime_capabilities_from_context(
        runtime_context,
        profile=RuntimeProfile.BACKGROUND_WORKER,
    ))
    if not capability_ids:
        return set(), set()
    return task_runtime_capability_tools(
        capability_ids,
        profile=RuntimeProfile.BACKGROUND_WORKER,
    ), capability_ids


def _as_dict(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return list(value) if isinstance(value, list) else []


async def _resolve_workspace_operation_tool_scope(
    db: AsyncSession | None,
    *,
    workspace_id: str | None,
    agent_id: str | None,
    agent_subscription_id: str | None,
    is_master: bool,
    task: Task | None,
    inherit_workspace_ledger_tools: bool = True,
    allow_workspace_ledger_writes: bool = True,
) -> tuple[set[str], set[str], set[str], bool]:
    """Overlay workspace-scoped operation bindings onto the turn scope.

    AgentToolBinding is agent-global today. Operation bindings are workspace
    scoped, so resolving them per turn avoids widening a reusable Agent's
    capabilities in other workspaces.
    """
    if not db or not workspace_id:
        return set(), set(), set(), False

    workspace = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if not workspace:
        return set(), set(), set(), False

    workspace_has_ledger = _settings_declare_ledger(workspace.settings)
    operating_model = _as_dict(workspace.operating_model)
    bindings = [
        dict(row)
        for row in _as_list(operating_model.get("capability_bindings"))
        if isinstance(row, dict) and row.get("enabled") is not False
    ]
    skill_bindings = [
        dict(row)
        for row in _as_list(operating_model.get("skill_bindings"))
        if isinstance(row, dict) and row.get("enabled") is not False
    ]
    needs_subscriptions = bool(bindings or skill_bindings) or bool(
        workspace_has_ledger
        and inherit_workspace_ledger_tools
        and not is_master
        and agent_id
    )
    subs = (
        list((await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.status == "active",
            )
        )).scalars().all())
        if needs_subscriptions
        else []
    )
    active_agent_ids = {
        str(sub.agent_id)
        for sub in subs
        if str(sub.agent_id or "").strip()
    }
    current_subscription = next(
        (
            sub for sub in subs
            if agent_subscription_id
            and sub.id == agent_subscription_id
            and str(sub.agent_id or "") == str(agent_id or "")
        ),
        None,
    )
    subscription_scope_supplied = bool(agent_subscription_id)
    inherits_workspace_ledgers = bool(
        workspace_has_ledger
        and inherit_workspace_ledger_tools
        and (
            is_master
            or (
                agent_id
                and str(agent_id) in active_agent_ids
                and (
                    not subscription_scope_supplied
                    or current_subscription is not None
                )
            )
        )
    )
    # Ledger contracts are Workspace capabilities. Internal subscribed service
    # agents inherit read/query/visualization access; only the master inherits
    # write tools. Public and external surfaces never receive this inheritance.
    ledger_tools = (
        workspace_ledger_runtime_tools(
            workspace.settings,
            include_write=is_master and allow_workspace_ledger_writes,
        )
        if inherits_workspace_ledgers
        else set()
    )
    if not bindings and not skill_bindings:
        return ledger_tools, set(), set(), workspace_has_ledger

    services_by_agent: dict[str, set[str]] = {}
    for sub in subs:
        service_key = str(sub.service_key or "").strip()
        if sub.agent_id and service_key:
            services_by_agent.setdefault(str(sub.agent_id), set()).add(service_key)
    subscription_agent_ids_by_id = {sub.id: sub.agent_id for sub in subs}
    task_service_keys = set(_task_service_keys(task)) if task else set()
    current_service_keys = (
        {
            str(current_subscription.service_key).strip()
        }
        if current_subscription
        and str(current_subscription.service_key or "").strip()
        else set()
        if subscription_scope_supplied
        else services_by_agent.get(str(agent_id or ""), set())
    )

    tool_names: set[str] = set(ledger_tools)
    mcp_tool_names: set[str] = set()
    capability_ids: set[str] = set()

    def _owner_matches(binding: dict) -> bool:
        return runtime_binding_owner_matches(
            binding,
            agent_id=agent_id,
            is_master=is_master,
            task_id=task.id if task else None,
            current_service_keys=current_service_keys,
            task_service_keys=task_service_keys,
            subscription_agent_ids_by_id=subscription_agent_ids_by_id,
            current_subscription_id=(
                current_subscription.id
                if current_subscription
                else ""
                if subscription_scope_supplied
                else None
            ),
        )

    for binding in bindings:
        if not _owner_matches(binding):
            continue
        expanded = expand_runtime_capability_binding(
            binding,
            profile=RuntimeProfile.WORKSPACE_OPERATOR,
        )
        tool_names.update(expanded.tool_names)
        mcp_tool_names.update(expanded.mcp_tool_names)
        capability_ids.update(expanded.capability_ids)
    if any(_owner_matches(binding) for binding in skill_bindings):
        tool_names.add("invoke_skill")
        capability_ids.add("skill.invoke")

    return tool_names, mcp_tool_names, capability_ids, workspace_has_ledger


async def _load_task_service_agent_ids(
    db: AsyncSession,
    *,
    entity_id: str | None,
    workspace_id: str | None,
    task: Task,
) -> list[str]:
    """Resolve the agents bound to the task's owner/delegate services."""
    service_keys = _task_service_keys(task)
    effective_entity_id = entity_id or task.entity_id
    if not service_keys or not effective_entity_id or not workspace_id:
        return []

    rows = (await db.execute(
        select(AgentSubscription.agent_id).where(
            AgentSubscription.entity_id == effective_entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
            AgentSubscription.service_key.in_(service_keys),
        )
    )).scalars().all()

    agent_ids: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        agent_id = str(raw or "").strip()
        if agent_id and agent_id not in seen:
            seen.add(agent_id)
            agent_ids.append(agent_id)
    return agent_ids


async def _resolve_service_agent_tool_scope(
    db: AsyncSession,
    service_agent_ids: list[str],
) -> tuple[
    set[str],
    set[str],
    tuple[RuntimeMCPProviderToolScope, ...],
]:
    """Union tool scopes from all service agents accountable for a task."""
    bound_tool_names: set[str] = set()
    mcp_allowed_names: set[str] = set()
    provider_scope_groups: list[tuple[RuntimeMCPProviderToolScope, ...]] = []
    for service_agent_id in service_agent_ids:
        service_bound, service_mcp, service_provider_scopes = await _resolve_agent_tool_scope(
            db,
            agent_id=service_agent_id,
            is_master=False,
        )
        if service_bound:
            bound_tool_names.update(service_bound)
        if service_mcp:
            mcp_allowed_names.update(service_mcp)
        provider_scope_groups.append(service_provider_scopes)
    return (
        bound_tool_names,
        mcp_allowed_names,
        RuntimeMCPProviderToolScopeFactory.merge(*provider_scope_groups),
    )


async def resolve_workspace_runtime(
    db: AsyncSession | None,
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    thread_ref_kind: str | None = None,
    thread_ref_id: str | None = None,
    is_master: bool | None = None,
    runtime_surface: ChatSurface | str | None = None,
    include_prompt_context: bool = True,
) -> WorkspaceRuntimeEnvelope:
    """Resolve one shared runtime envelope for a turn.

    This function intentionally does not execute any tools. It only decides
    which workspace/task context exists and which tool surface should be
    visible before an LLM turn starts.
    """
    runtime_context = await load_conversation_runtime_context(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
    )
    workspace_id = workspace_id or runtime_context.get("workspace_id")
    agent_subscription_id = (
        agent_subscription_id
        or runtime_context.get("agent_subscription_id")
    )
    task_id = task_id or runtime_context.get("task_id")
    thread_ref_kind = thread_ref_kind or runtime_context.get("thread_ref_kind")
    thread_ref_id = thread_ref_id or runtime_context.get("thread_ref_id")
    extra_context = runtime_context.get("extra_context") if include_prompt_context else None
    if include_prompt_context:
        pending_hitl_context = await _pending_hitl_extra_context(
            db,
            conversation_id=conversation_id,
        )
        if pending_hitl_context:
            extra_context = "\n\n".join(
                part for part in [extra_context, pending_hitl_context] if part
            )

    task: Task | None = None
    if db and task_id:
        task = await _load_task_for_runtime(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        if task:
            workspace_id = workspace_id or task.workspace_id
            thread_ref_kind = thread_ref_kind or "task"
            thread_ref_id = thread_ref_id or task.id
            if include_prompt_context and not extra_context:
                extra_context = _task_extra_context(task)

    if include_prompt_context:
        task_blocker_context = await _open_task_blockers_extra_context(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        if task_blocker_context:
            extra_context = "\n\n".join(
                part for part in [extra_context, task_blocker_context] if part
            )

    resolved_is_master = (
        bool(is_master)
        if is_master is not None
        else (not agent_id or is_master_agent(agent_id, None))
    )
    turn_profiles = runtime_workspace_turn_profile_names(workspace_id)
    runtime_profile = turn_profiles.runtime_profile
    tool_profile = turn_profiles.tool_profile
    bound_tool_names, mcp_allowed_names, mcp_provider_scopes = await _resolve_agent_tool_scope(
        db,
        agent_id=agent_id,
        is_master=resolved_is_master,
    )
    agent_capabilities: set[str] = set()
    normalized_surface: ChatSurface | None = None
    if isinstance(runtime_surface, ChatSurface):
        normalized_surface = runtime_surface
    elif runtime_surface:
        try:
            normalized_surface = ChatSurface(str(runtime_surface))
        except ValueError:
            normalized_surface = None
    if not resolved_is_master:
        has_visible_skills = await _agent_has_visible_skills(
            db,
            entity_id=entity_id,
            agent_id=agent_id,
        )
        if has_visible_skills and normalized_surface == ChatSurface.PUBLIC_CUSTOMER_CHAT:
            try:
                from packages.core.ai.runtime.skills import runtime_agent_has_surface_bound_skill

                has_visible_skills = await runtime_agent_has_surface_bound_skill(
                    db,
                    entity_id=entity_id,
                    agent_id=agent_id,
                    surface=normalized_surface,
                    profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
                    allowed_tool_names=bound_tool_names,
                )
            except Exception:
                logger.warning(
                    "Runtime public skill scope resolution failed for agent %s; default-deny",
                    agent_id,
                    exc_info=True,
                )
                has_visible_skills = False
    else:
        has_visible_skills = False
    if has_visible_skills:
        bound_tool_names = set(bound_tool_names or set())
        bound_tool_names.add("invoke_skill")
        agent_capabilities.add("skill.invoke")
    service_agent_ids: list[str] = []
    if db and task and workspace_id and resolved_is_master:
        service_agent_ids = await _load_task_service_agent_ids(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task=task,
        )
        service_bound, service_mcp, service_provider_scopes = await _resolve_service_agent_tool_scope(
            db,
            service_agent_ids,
        )
        # The presence of accountable service Agents closes the Host's MCP
        # scope even when their current union is empty. Otherwise revoking the
        # last service-Agent MCP binding would turn ``None`` back into the
        # master's legacy global wildcard.
        if mcp_allowed_names is None:
            mcp_allowed_names = set()
        if service_bound:
            bound_tool_names = set(bound_tool_names or set())
            bound_tool_names.update(service_bound)
        if service_mcp:
            mcp_allowed_names = set(mcp_allowed_names or set())
            mcp_allowed_names.update(service_mcp)
        mcp_provider_scopes = RuntimeMCPProviderToolScopeFactory.merge(
            mcp_provider_scopes,
            service_provider_scopes,
        )

    task_bound, task_capabilities = _task_runtime_capability_scope(task)
    if task_bound:
        bound_tool_names = set(bound_tool_names or set())
        bound_tool_names.update(task_bound)

    allow_workspace_ledger_writes = True
    if (
        db
        and workspace_id
        and resolved_is_master
        and normalized_surface not in {
            ChatSurface.SCHEDULED_AGENT_RUN,
            ChatSurface.WORKFLOW_AGENT_STEP,
        }
        and (user_id is not None or normalized_surface is not None)
    ):
        allow_workspace_ledger_writes = bool(
            user_id
            and await user_can_write_workspace_artifacts(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        )

    operation_scope = await _resolve_workspace_operation_tool_scope(
        db,
        workspace_id=workspace_id,
        agent_id=agent_id,
        agent_subscription_id=agent_subscription_id,
        is_master=resolved_is_master,
        task=task,
        inherit_workspace_ledger_tools=normalized_surface not in {
            ChatSurface.PUBLIC_CUSTOMER_CHAT,
            ChatSurface.EXTERNAL_CHANNEL_CHAT,
        },
        allow_workspace_ledger_writes=allow_workspace_ledger_writes,
    )
    # Keep compatibility with older test/integration adapters that still
    # return the pre-ledger three-tuple while they are being upgraded.
    if len(operation_scope) == 3:
        operation_bound, operation_mcp, operation_capabilities = operation_scope
        workspace_has_ledger = False
    else:
        operation_bound, operation_mcp, operation_capabilities, workspace_has_ledger = operation_scope
    if operation_bound:
        bound_tool_names = set(bound_tool_names or set())
        bound_tool_names.update(operation_bound)
    if operation_mcp:
        mcp_allowed_names = set(mcp_allowed_names or set())
        mcp_allowed_names.update(operation_mcp)
    if workspace_has_ledger:
        bound_tool_names = runtime_expand_workspace_ledger_tools(bound_tool_names)
    elif (
        resolved_is_master
        and workspace_id
        and normalized_surface == ChatSurface.WORKSPACE_CHAT
    ):
        # Keep manual Ledger setup reachable through the conversation even
        # before the Workspace has an installed contract. The visualization
        # tool returns an empty overview whose host-owned action opens config.
        bound_tool_names = set(bound_tool_names or set())
        bound_tool_names.add(WORKSPACE_LEDGER_VISUALIZATION_TOOL)

    # ``is_master`` identifies the Host; it is not an MCP permission. Once a
    # Task/service Agent contributes semantic provider authority, close the
    # legacy exact-name channel even when that provider currently has no
    # registered schemas. Dynamic discovery can then add only names admitted
    # by ``mcp_provider_scopes`` instead of inheriting every global MCP tool.
    if mcp_provider_scopes and mcp_allowed_names is None:
        mcp_allowed_names = set()

    return WorkspaceRuntimeEnvelope(
        workspace_id=workspace_id,
        task_id=task_id,
        thread_ref_kind=thread_ref_kind,
        thread_ref_id=thread_ref_id,
        runtime_profile=runtime_profile,
        tool_profile=tool_profile,
        extra_context=extra_context,
        is_master=resolved_is_master,
        bound_tool_names=bound_tool_names,
        mcp_allowed_names=mcp_allowed_names,
        mcp_provider_scopes=mcp_provider_scopes,
        mcp_scope_unrestricted=(
            resolved_is_master
            and mcp_allowed_names is None
            and not mcp_provider_scopes
        ),
        capability_ids=agent_capabilities | set(operation_capabilities) | set(task_capabilities),
        service_agent_ids=service_agent_ids,
    )


async def ensure_workspace_task_conversation(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
    title: str | None = None,
) -> Conversation:
    """Create/reuse the workspace thread that represents a task."""
    from packages.core.workspace_chat.service import spawn_thread

    conv = await spawn_thread(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        thread_ref_kind="task",
        thread_ref_id=task_id,
        title=title,
    )
    from packages.core.services.task_session import bind_task_session_conversation

    await bind_task_session_conversation(db, conv)
    return conv


async def process_workspace_task_comment(
    *,
    task_id: str,
    entity_id: str,
    user_id: str | None,
    author_label: str,
    comment: str,
    log_id: str | None = None,
    responding_agent_id: str | None = None,
) -> None:
    """Turn a task comment into a Workspace Agent turn.

    The comment is mirrored into the task's workspace thread, then the normal
    non-streaming chat runtime processes it. Tool policy still gates any side
    effects, so this remains safe even when the comment asks for an action.

    When ``responding_agent_id`` is provided (an @mentioned agent), that
    agent handles the turn with its own capabilities; otherwise the
    task's assigned agent (``task.agent_id``) responds as before.
    """
    comment = (comment or "").strip()
    if not comment:
        return

    from packages.core.database import async_session
    from packages.core.services.conversation_messages import add_message
    from packages.core.ai.runtime import runtime_run_chat_turn
    from packages.core.services.task_service import add_task_log, agent_log_authorship

    conversation_id: str | None = None
    workspace_id: str | None = None

    try:
        async with async_session() as db:
            task = await _load_task_for_runtime(
                db,
                entity_id=entity_id,
                workspace_id=None,
                task_id=task_id,
            )
            if not task or not task.workspace_id:
                return

            workspace_id = task.workspace_id
            from packages.core.services.task_session import resolve_task_session_host

            task_session_host = await resolve_task_session_host(db, task)
            if task_session_host:
                # Interactive sessions are always conducted by their Task Host;
                # mentions do not transfer the turn to another Agent.
                responding_agent_id = task_session_host.agent_id
            elif responding_agent_id is None:
                # Not just task.agent_id: plan-driven work resolves an agent
                # per step and never copies it up, so a reply would land on
                # the master agent instead of the one that produced the work.
                from packages.core.services.task_service import (
                    task_executing_agent_id,
                )

                responding_agent_id = await task_executing_agent_id(db, task)
            conv = await ensure_workspace_task_conversation(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task.id,
                title=task.title,
            )
            conversation_id = conv.id
            if not task.conversation_id:
                task.conversation_id = conv.id

            await add_message(
                db,
                conv.id,
                role="user",
                content=comment,
                meta={
                    "source": "task_comment",
                    "task_id": task.id,
                    "task_log_id": log_id,
                    "author_label": author_label,
                },
            )
            await db.commit()

        async with async_session() as db:
            result = await runtime_run_chat_turn(
                comment,
                conversation_id,
                surface=ChatSurface.TASK_COMMENT_THREAD,
                entity_id=entity_id,
                user_id=user_id,
                agent_id=responding_agent_id,
                workspace_id=workspace_id,
                db=db,
            )
            content = str((result or {}).get("content") or "").strip()
            if content:
                created_by, author_meta, author_actor = await agent_log_authorship(
                    db, responding_agent_id,
                )
                await add_task_log(
                    db,
                    task_id,
                    TaskLogType.WORKSPACE_AGENT_RESPONSE,
                    content,
                    actor=author_actor,
                    created_by=created_by,
                    metadata={
                        **(author_meta or {}),
                        "source": "task_comment",
                        "conversation_id": conversation_id,
                        "message_id": (result or {}).get("message_id"),
                        "tool_calls_made": (result or {}).get("tool_calls_made") or [],
                    },
                )
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Workspace Agent task comment processing failed: task=%s error=%s",
            task_id,
            exc,
            exc_info=True,
        )
        try:
            async with async_session() as db:
                created_by, author_meta, author_actor = await agent_log_authorship(
                    db, responding_agent_id,
                )
                await add_task_log(
                    db,
                    task_id,
                    TaskLogType.WORKSPACE_AGENT_ERROR,
                    f"Workspace Agent could not process comment: {exc}",
                    actor=author_actor,
                    created_by=created_by,
                    metadata={**(author_meta or {}), "source": "task_comment", "task_log_id": log_id},
                )
                await db.commit()
        except Exception:
            logger.debug("Failed to persist workspace task comment error", exc_info=True)


async def process_workspace_chat_message(
    *,
    conversation_id: str,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    message: str,
    message_id: str | None = None,
) -> None:
    """Turn a Workspace Chat user message into a Workspace Agent turn.

    Workspace Chat stores the user's message synchronously so the UI is honest
    even if the model path is slow. This async follow-up runs the same
    agentic runtime used by task comments; tool policy and HITL still gate
    side effects, so chat instructions can safely produce drafts, operation
    proposals, tasks, rules, or Strategist review requests.
    """
    message = (message or "").strip()
    if not message:
        return

    from packages.core.database import async_session
    from packages.core.ai.runtime import runtime_run_chat_turn
    from packages.core.workspace_chat import service as workspace_chat_service

    try:
        async with async_session() as db:
            await runtime_run_chat_turn(
                message,
                conversation_id,
                surface=ChatSurface.WORKSPACE_CHAT,
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=workspace_id,
                db=db,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Workspace Agent chat processing failed: workspace=%s message=%s error=%s",
            workspace_id,
            message_id,
            exc,
            exc_info=True,
        )
        try:
            async with async_session() as db:
                await workspace_chat_service.post_message(
                    db,
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    body=f"Workspace Agent could not process this message: {exc}",
                    message_kind="agent_error",
                    author_kind="agent",
                    refs=([{"type": "message", "id": message_id}] if message_id else None),
                )
                await db.commit()
        except Exception:
            logger.debug("Failed to persist workspace chat processing error", exc_info=True)


async def install_workspace_runtime_schedules(
    db: AsyncSession,
    workspace: Workspace,
    *,
    cadence: str | None = None,
) -> None:
    """Install/refresh all built-in workspace runtime schedules.

    Workspace heartbeat is the single autonomous-runtime switch.  Goal
    measurement jobs are derived from Goal rows, so they are refreshed here
    alongside the Strategist/evolution jobs instead of being left running
    independently of the Workspace switch.
    """
    effective_cadence = cadence or workspace.heartbeat_cadence or "daily"
    workspace.heartbeat_cadence = effective_cadence

    from packages.core.strategist.evolution_scheduling import install_evolution_schedules
    from packages.core.strategist.scheduling import install_strategist_schedule

    await install_strategist_schedule(db, workspace, cadence=effective_cadence)
    await install_evolution_schedules(db, workspace)
    await _sync_workspace_goal_schedules(db, workspace, enabled=True)


async def remove_workspace_runtime_schedules(
    db: AsyncSession,
    workspace_id: str,
    *,
    entity_id: str | None = None,
) -> None:
    """Remove all built-in workspace runtime schedules.

    ``entity_id`` is optional for compatibility with existing callers.  When
    supplied, automatic Goal measurement jobs are removed with the Strategist
    jobs so disabling the Workspace runtime cannot leave goal polling active.
    """
    from packages.core.strategist.evolution_scheduling import remove_evolution_schedules
    from packages.core.strategist.scheduling import remove_strategist_schedule

    await remove_strategist_schedule(db, workspace_id)
    await remove_evolution_schedules(db, workspace_id)
    if entity_id:
        from sqlalchemy import select
        from packages.core.models.goal import Goal

        goals = list((await db.execute(
            select(Goal).where(
                Goal.entity_id == entity_id,
                Goal.workspace_id == workspace_id,
            )
        )).scalars().all())
        await _sync_workspace_goal_schedules(
            db,
            workspace=None,
            goals=goals,
            enabled=False,
        )


async def sync_workspace_runtime_schedules(
    db: AsyncSession,
    workspace: Workspace,
) -> None:
    """Make scheduler rows match the workspace's runtime state."""
    autonomy_state = workspace_autonomy_state(workspace)
    if autonomy_state is WorkspaceAutonomyState.PAUSED:
        # Keep automation definitions visible and restorable while making the
        # paused state explicit in the Automations UI. This also covers the
        # built-in Strategist/evolution jobs instead of deleting them.
        from packages.core.services.scheduler_service import pause_workspace_automations

        await pause_workspace_automations(db, workspace.id, workspace.entity_id)
        return
    if autonomy_state is WorkspaceAutonomyState.RUNNING:
        await install_workspace_runtime_schedules(db, workspace)
    else:
        await remove_workspace_runtime_schedules(
            db,
            workspace.id,
            entity_id=workspace.entity_id,
        )


async def _sync_workspace_goal_schedules(
    db: AsyncSession,
    workspace: Workspace | None,
    *,
    enabled: bool,
    goals: list[Any] | None = None,
) -> None:
    """Make automatic Goal measurement jobs follow the Workspace runtime.

    Manual goals never get a job. Disabling removes derived ``gm:`` jobs
    and linked Stat collectors; user-authored automations remain untouched.
    """
    from packages.core.goals.scheduling import (
        install_measurement_schedule,
        remove_measurement_schedule,
        should_install_measurement_schedule,
    )
    if goals is None:
        if workspace is None:
            return
        from sqlalchemy import select
        from packages.core.models.goal import Goal

        goals = list((await db.execute(
            select(Goal).where(
                Goal.entity_id == workspace.entity_id,
                Goal.workspace_id == workspace.id,
            )
        )).scalars().all())

    for goal in goals:
        if enabled and should_install_measurement_schedule(goal):
            await install_measurement_schedule(db, goal)
        else:
            await remove_measurement_schedule(db, goal)

    # A Goal measured by a Stat has no separate gm: job. Keep its collector
    # on the same runtime switch, including off -> on and pause -> resume.
    stat_ids = {goal.stat_id for goal in goals if getattr(goal, "stat_id", None)}
    if stat_ids:
        from sqlalchemy import select
        from packages.core.models.workspace_stat import WorkspaceStat
        from packages.core.stats.scheduling import (
            remove_stat_collection_schedule,
            sync_stat_collection_schedule,
        )

        scopes = {(goal.stat_id, goal.entity_id, goal.workspace_id) for goal in goals if getattr(goal, "stat_id", None)}
        stats = (await db.execute(select(WorkspaceStat).where(
            WorkspaceStat.id.in_(stat_ids),
        ))).scalars().all()
        for stat in stats:
            if (stat.id, stat.entity_id, stat.workspace_id) not in scopes:
                continue
            if enabled:
                await sync_stat_collection_schedule(db, stat)
            else:
                await remove_stat_collection_schedule(db, stat)
