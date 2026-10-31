"""Task endpoints — CRUD, status changes, processing logs, automation."""
from __future__ import annotations

import logging
import asyncio
from datetime import timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.pending_actions import PendingActionKind
from packages.core.database import get_db
from packages.core.models.task import Task, TaskLog
from packages.core.models.user import User
from packages.core.constants.task_actors import TaskActor
from packages.core.constants.task import TaskLogType, TaskStatus, TaskType, TASK_STATUSES, TASK_PRIORITIES, TASK_CATEGORIES, TASK_TYPES
from packages.core.constants.task_notifications import (
    task_notification_channels,
    task_notification_events,
)
from packages.core.services.task_service import (
    list_tasks, get_task, create_task, update_task, ensure_workspace_agent_assignment,
    ensure_workspace_owner_subscription,
    add_task_log, get_task_logs,
    list_categories, create_category, update_category, delete_category,
    get_tasks_by_status, move_task,
    list_sla_policies, create_sla_policy,
    update_sla_policy, delete_sla_policy,
)
from packages.core.services.workspace_runtime import process_workspace_task_comment
from packages.core.services.task_comment_mentions import (
    notify_mentioned_users,
    validate_mentions,
)
from packages.core.services.task_state_machine import (
    TaskStatusTransitionError,
)
from packages.core.services.settings_service import update_user_preferences
from apps.api.deps import (
    get_current_user,
    require_workspace_authority,
    require_workspace_readable,
    require_workspace_writable,
)

logger = logging.getLogger(__name__)

_TASK_RUNTIME_DETAIL_KEYS = frozenset({
    "_pending_plan_dispatch",
    "_replan_context",
    "_manual_execution",
    "automation_origin",
    "scheduled_job_id",
    "scheduled_run_id",
    "strategist_review_id",
    "customer_context",
    "proposal_external_authorization",
    "runtime_context",
    "capability_blocker",
    "workspace_operation_draft_id",
    "workspace_work_batch_id",
})

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TASK_BOARD_COLUMNS = ("todo", "scheduled", "in_progress", "review", "done")
_TASK_BOARD_PREFERENCES_KEY = "task_board"
_MAX_REQUIRED_SKILLS = 10


async def _required_skill_refs(db: AsyncSession, user: User, refs: list[str]) -> list[str]:
    """Canonical refs (slug, else id) of Skills the caller can see, or 422.

    A ref is resolved as ``invoke_skill`` resolves it (an id, else a slug of
    this entity or a built-in), then authorized like a Skill read: built-ins
    are readable by every entity, an entity Skill through the resource
    gateway. Unknown and invisible refs get the same answer.
    """
    from packages.core.models.permission import Capability, ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, user_can_access_resource
    from packages.core.services.skill_service import get_skill, get_skill_by_slug

    canonical: list[str] = []
    rejected: list[str] = []
    for ref in dict.fromkeys(str(value or "").strip() for value in refs):
        if not ref:
            continue
        skill = await get_skill(db, ref)
        if skill is None or skill.entity_id not in {None, user.entity_id}:
            skill = await get_skill_by_slug(db, ref, user.entity_id)
        visible = skill is not None and skill.status == "active" and (
            skill.entity_id is None
            or await user_can_access_resource(
                db, descriptor=ResourceDescriptor.from_row(skill, ResourceType.SKILL),
                entity_id=user.entity_id, user_id=user.id, role=getattr(user, "role", None),
                capability=Capability.VIEW,
            )
        )
        if not visible:
            rejected.append(ref)
        else:
            canonical.append(skill.slug or skill.id)
    if rejected:
        raise HTTPException(422, f"Unknown or inaccessible Skills in required_skills: {rejected}")
    return list(dict.fromkeys(canonical))


def _schedule_background(coro) -> None:
    """Fire-and-forget without making the user's HTTP action wait."""
    task = asyncio.get_running_loop().create_task(coro)

    def _log_failure(done: asyncio.Task) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.warning("Task comment background processing failed", exc_info=True)

    task.add_done_callback(_log_failure)


def _schedule_workspace_task_comment_processing(
    *, responder_agent_ids: list | None = None, **kwargs,
) -> None:
    """Run workspace-agent follow-ups serially (they share the task thread)."""

    async def _run_all() -> None:
        for agent_id in (responder_agent_ids or [None]):
            await process_workspace_task_comment(
                responding_agent_id=agent_id, **kwargs,
            )

    _schedule_background(_run_all())


def _is_attachment_only_comment(content: str | None, attachments: list[dict] | None) -> bool:
    """Skip background AI on UI-generated attachment placeholder comments."""
    if not attachments:
        return False
    text = (content or "").strip().lower()
    return bool(text) and text.startswith("attached ") and " file" in text


async def _record_task_user_decision_evidence(
    db: AsyncSession,
    *,
    user: User,
    task,
    evidence_type: str,
    summary: str,
    details: dict,
    metrics: dict | None = None,
) -> list[str]:
    """Best-effort runtime evidence for task-page user decisions."""
    try:
        from packages.core.services.runtime_learning import (
            queued_learning_candidate_ids,
            record_user_signal_evidence,
        )

        _evidence, candidates = await record_user_signal_evidence(
            db,
            entity_id=user.entity_id,
            workspace_id=task.workspace_id,
            agent_id=getattr(task, "agent_id", None),
            user_id=user.id,
            task_id=task.id,
            evidence_type=evidence_type,
            source="task_ui",
            status="succeeded",
            summary=summary,
            details={
                "task_title": task.title,
                "task_type": task.task_type,
                **details,
            },
            metrics=metrics or {},
            guidance_text=_task_user_guidance_text(details),
        )
        return queued_learning_candidate_ids(candidates)
    except Exception:
        logger.debug("task user decision runtime evidence skipped", exc_info=True)
        return []


def _task_user_guidance_text(details: dict) -> str:
    parts: list[str] = []
    for key in ("comment", "note", "response"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _merge_task_details(existing: dict | None, incoming: dict | None) -> dict:
    """Patch task details without dropping runtime metadata.

    Workspace task batches, operation drafts, and runtime_context all live in
    the details JSONB payload. A generic task update should add user fields
    without silently severing the task from its workspace runtime.
    """
    merged = dict(existing or {})
    merged.update(dict(incoming or {}))
    return merged


def _without_task_runtime_details(details: dict | None) -> dict:
    return {
        key: value
        for key, value in dict(details or {}).items()
        if key not in _TASK_RUNTIME_DETAIL_KEYS
    }


async def _enqueue_learning_candidate_applies(
    db: AsyncSession,
    *,
    user: User,
    workspace_id: str | None,
    candidate_ids: list[str],
) -> None:
    ids = list(dict.fromkeys(candidate_ids or []))
    if not ids:
        return
    try:
        from packages.core.services.runtime_learning import enqueue_learning_candidate_apply

        has_enqueue_failure = False
        for candidate_id in ids:
            failed_row = await enqueue_learning_candidate_apply(
                db,
                entity_id=user.entity_id,
                candidate_id=candidate_id,
                workspace_id=workspace_id,
                user_id=user.id,
            )
            has_enqueue_failure = has_enqueue_failure or failed_row is not None
        if has_enqueue_failure:
            await db.commit()
    except Exception:
        logger.warning("Failed to enqueue task learning candidate apply", exc_info=True)


# ── Schemas ──

class TaskResponse(BaseModel):
    id: str
    entity_id: str
    title: str
    description: str | None = None
    status: str
    priority: int
    task_type: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    category_id: str | None = None
    assignee_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    owner_service_key: str | None = None
    owner_subscription_id: str | None = None
    owner_agent_id: str | None = None
    owner_agent_name: str | None = None
    owner_agent_avatar: str | None = None
    current_blocker: dict | None = None
    session_host_agent_id: str | None = None
    session_host_subscription_id: str | None = None
    session_host_name: str | None = None
    session_host_avatar: str | None = None
    session_host_role_label: str | None = None
    session_host_available: bool | None = None
    session_host_error: str | None = None
    creator_id: str | None = None
    conversation_id: str | None = None
    parent_task_id: str | None = None
    required_skills: list[str] = []
    estimated_hours: float | None = None
    visibility: str = "entity"
    owner_id: str | None = None
    client_visible: bool = False
    sla_policy_id: str | None = None
    sla_breached: bool = False
    escalation_level: int = 0
    details: dict = {}
    actual_output: dict | None = None
    deadline: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    status_changed_at: str | None = None
    execution_active: bool = False
    # Resolved display fields (populated by backend, not stored in DB)
    assignee_name: str | None = None
    assignee_avatar: str | None = None
    agent_name: str | None = None
    agent_avatar: str | None = None
    creator_name: str | None = None
    creator_avatar: str | None = None
    author_agent_id: str | None = None
    author_agent_name: str | None = None
    author_agent_avatar: str | None = None
    # An external Workspace collaborator steered this Task: its runs stay in
    # their Workspace, and stop once their access ends until a member re-saves it.
    steered_by_collaborator: bool = False


class TaskCreateRequest(BaseModel):
    title: str
    description: str = ""
    priority: int = 3
    task_type: str = "general"
    workspace_id: str | None = None
    category_id: str | None = None
    assignee_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    owner_service_key: str | None = None
    owner_subscription_id: str | None = None
    # Skill ids or slugs this Task carries out (merged with its Workspace
    # service's required_skills). Each must be a Skill the caller can see.
    required_skills: list[str] | None = Field(default=None, max_length=_MAX_REQUIRED_SKILLS)
    details: dict = {}
    deadline: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None


# Saving any of these is a member's review of the Task (see update_one_task).
_TASK_CONTENT_FIELDS = frozenset({
    "title", "description", "details", "assignee_id", "agent_id", "agent_type",
    "owner_service_key", "owner_subscription_id",
})


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    assignee_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    owner_service_key: str | None = None
    owner_subscription_id: str | None = None
    category_id: str | None = None
    sla_policy_id: str | None = None
    # Replaces the Task's required Skills; [] clears them.
    required_skills: list[str] | None = Field(default=None, max_length=_MAX_REQUIRED_SKILLS)
    details: dict | None = None
    deadline: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int


class TaskBoardPreferencesRequest(BaseModel):
    mode: Literal["kanban", "list"] = "kanban"
    visible_columns: list[str] = Field(default_factory=lambda: list(_TASK_BOARD_COLUMNS))
    column_order: list[str] = Field(default_factory=lambda: list(_TASK_BOARD_COLUMNS))


class TaskBoardPreferencesResponse(TaskBoardPreferencesRequest):
    configured: bool = False


class TaskLogResponse(BaseModel):
    id: str
    task_id: str
    log_type: str
    content: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    attachments: list[dict] = []
    # Author hints for the frontend — present when the log was posted
    # by an identifiable agent (auto-summary, AI turns, supervisor
    # verdict). Lets the UI render the actual agent's avatar + name
    # instead of a generic "AI Agent" placeholder.
    author_agent_id: str | None = None
    author_agent_name: str | None = None
    # Free-form ``meta`` is also surfaced — UI uses fields like
    # ``meta.question`` for HITL pull-outs.
    meta: dict | None = None


class AddLogRequest(BaseModel):
    content: str
    # Typed against the log vocabulary: an unknown type is a 422 here rather
    # than a row the frontend has no icon (and no meaning) for.
    log_type: TaskLogType = TaskLogType.COMMENT
    attachments: list[dict] = []
    mentions: list[dict] = Field(default_factory=list, max_length=50)
    request_id: str | None = Field(default=None, max_length=26)


class CategoryResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    icon: str | None = None
    color: str | None = None
    sort_order: int = 0


class CategoryCreateRequest(BaseModel):
    name: str
    icon: str | None = None
    color: str | None = None
    sort_order: int = 0


class CategoryUpdateRequest(BaseModel):
    name: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int | None = None


class MoveTaskRequest(BaseModel):
    status: str


class RetryTaskRequest(BaseModel):
    note: str | None = None


class ApprovalDecisionRequest(BaseModel):
    choice: str
    note: str | None = None


class RetryTaskResponse(BaseModel):
    task: TaskResponse
    dispatched: bool
    mode: str


class HITLResponseRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=26)
    response: str | None = None
    choice: str | None = None
    fields: dict = {}
    note: str | None = None


class HITLResponseResponse(BaseModel):
    task: TaskResponse
    resumed: bool
    dispatched: bool
    mode: str | None = None


# ── Constants endpoint ──

@router.get("/constants")
async def get_task_constants():
    """Return task statuses, priorities, categories, and types for frontend."""
    from packages.core.services.task_state_machine import status_transition_map
    return {
        "statuses": TASK_STATUSES,
        "status_transitions": status_transition_map(),
        "priorities": TASK_PRIORITIES,
        "categories": TASK_CATEGORIES,
        "types": TASK_TYPES,
        "notification_channels": task_notification_channels(),
        "notification_events": task_notification_events(),
    }


class EscalateResponse(BaseModel):
    task_id: str
    escalated: bool


class ReassignResponse(BaseModel):
    task_id: str
    new_assignee_id: str | None = None


class TaskFromTemplateRequest(BaseModel):
    template_id: str
    title: str | None = None
    description: str | None = None
    priority: int | None = None
    task_type: str | None = None
    category_id: str | None = None
    assignee_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    details: dict | None = None
    deadline: str | None = None


class ChecklistItemResponse(BaseModel):
    id: str
    task_id: str
    content: str
    is_completed: bool
    sort_order: int


class AddChecklistItemRequest(BaseModel):
    content: str
    sort_order: int | None = None


class ToggleChecklistItemRequest(BaseModel):
    is_completed: bool


def _to_checklist_response(item) -> ChecklistItemResponse:
    return ChecklistItemResponse(
        id=item.id,
        task_id=item.task_id,
        content=item.content,
        is_completed=item.is_completed,
        sort_order=item.sort_order,
    )


async def _resolve_lookups(
    db: AsyncSession,
    tasks,
) -> tuple[
    dict,
    dict,
    dict,
    dict,
    dict[str, str | None],
    dict[str, str | None],
]:
    """Batch-load scoped user, staff, agent, and workspace display info for tasks."""
    from packages.core.models.staff import Staff
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    from packages.core.constants.agents import MANOR_AGENT_IDS

    entity_ids = {t.entity_id for t in tasks if getattr(t, "entity_id", None)}
    workspace_ids = {t.workspace_id for t in tasks if getattr(t, "workspace_id", None)}
    assignee_ids = {t.assignee_id for t in tasks if t.assignee_id}
    from packages.core.services.task_requester_identity import resolve_task_requesters

    requester_resolutions = await resolve_task_requesters(db, tasks)
    requester_ids = {
        task_id: resolution.user_id
        for task_id, resolution in requester_resolutions.items()
    }
    author_agent_ids = {
        task_id: resolution.author_agent_id
        for task_id, resolution in requester_resolutions.items()
    }
    creator_ids = {user_id for user_id in requester_ids.values() if user_id}
    actor_ids = assignee_ids | creator_ids
    user_ids = set(actor_ids)
    # Filter out manor-master — it's not a real DB agent.
    explicit_agent_ids = {t.agent_id for t in tasks if t.agent_id and t.agent_id not in MANOR_AGENT_IDS}
    owner_subscription_ids = {
        t.owner_subscription_id
        for t in tasks
        if getattr(t, "owner_subscription_id", None)
    }
    owner_service_scopes = {
        (t.entity_id, t.workspace_id, t.owner_service_key)
        for t in tasks
        if t.workspace_id
        and getattr(t, "owner_service_key", None)
        and not getattr(t, "owner_subscription_id", None)
    }

    users: dict = {}
    agents: dict = {}
    staff: dict = {}
    workspaces: dict = {}
    subscription_hosts: dict[str, dict] = {}

    try:
        if workspace_ids and entity_ids:
            result = await db.execute(
                select(Workspace).where(
                    Workspace.id.in_(workspace_ids),
                    Workspace.entity_id.in_(entity_ids),
                    Workspace.deleted_at.is_(None),
                )
            )
            for w in result.scalars():
                workspaces[w.id] = {"name": w.name}

        if user_ids and entity_ids:
            result = await db.execute(
                select(User)
                .outerjoin(
                    UserMembership,
                    UserMembership.user_id == User.id,
                )
                .where(
                    User.id.in_(user_ids),
                    User.deleted_at.is_(None),
                    or_(
                        User.entity_id.in_(entity_ids),
                        UserMembership.entity_id.in_(entity_ids),
                    ),
                )
                .distinct()
            )
            for u in result.scalars():
                users[u.id] = {"name": u.display_name or u.email, "avatar_url": getattr(u, "avatar_url", None)}

        # Some task flows store a staff ULID in assignee_id or creator_id
        # (auto-reassign, manor.assign_task, and older workspace workflows).
        # Resolve those alongside users so clients never have to show opaque ids.
        if actor_ids and entity_ids:
            result = await db.execute(
                select(Staff).where(
                    Staff.entity_id.in_(entity_ids),
                    Staff.id.in_(actor_ids),
                    Staff.deleted_at.is_(None),
                )
            )
            for s in result.scalars():
                row = {"name": s.name or s.email, "avatar_url": getattr(s, "avatar_url", None)}
                staff[s.id] = row
                if s.user_id:
                    staff.setdefault(s.user_id, row)

            # Workspace owner auto-assignment / requester attribution can use
            # the owner's entity id. Map that entity id back to owner/admin.
            unresolved_actor_ids = actor_ids - set(users) - set(staff)
            if unresolved_actor_ids:
                result = await db.execute(
                    select(User).where(
                        User.entity_id.in_(unresolved_actor_ids),
                        User.entity_id.in_(entity_ids),
                        User.role.in_(("owner", "admin")),
                        User.deleted_at.is_(None),
                    )
                )
                for u in result.scalars():
                    row = {"name": u.display_name or u.email, "avatar_url": getattr(u, "avatar_url", None)}
                    users.setdefault(u.entity_id, row)

        if (owner_subscription_ids or owner_service_scopes) and entity_ids:
            result = await db.execute(
                select(AgentSubscription).where(
                    AgentSubscription.entity_id.in_(entity_ids),
                    AgentSubscription.status == "active",
                    or_(
                        AgentSubscription.id.in_(owner_subscription_ids),
                        *[
                            and_(
                                AgentSubscription.entity_id == entity_id,
                                AgentSubscription.workspace_id == workspace_id,
                                AgentSubscription.service_key == service_key,
                            )
                            for entity_id, workspace_id, service_key in owner_service_scopes
                        ],
                    ),
                )
            )
            for subscription in result.scalars():
                subscription_hosts[subscription.id] = {
                    "subscription_id": subscription.id,
                    "agent_id": subscription.agent_id,
                    "subscription_entity_id": subscription.entity_id,
                    "workspace_id": subscription.workspace_id,
                    "service_key": subscription.service_key,
                    "role_label": subscription.name or subscription.service_key,
                }

        # Normal agent assignment uses Task.agent_id. Be permissive for older
        # rows that stored an agent id in assignee_id, while still scoping
        # display resolution to this entity or public template agents.
        possible_agent_ids = (
            explicit_agent_ids
            | actor_ids
            | {
                host["agent_id"]
                for host in subscription_hosts.values()
                if host.get("agent_id")
            }
            | {agent_id for agent_id in author_agent_ids.values() if agent_id}
        ) - MANOR_AGENT_IDS
        if possible_agent_ids and entity_ids:
            result = await db.execute(
                select(Agent).where(
                    Agent.id.in_(possible_agent_ids),
                    Agent.deleted_at.is_(None),
                    or_(
                        Agent.entity_id.in_(entity_ids),
                        and_(Agent.entity_id.is_(None), Agent.is_public.is_(True)),
                    ),
                )
            )
            for a in result.scalars():
                agents[a.id] = {
                    "entity_id": a.entity_id,
                    "name": a.name,
                    "avatar_url": getattr(a, "avatar_url", None),
                    "available": a.status == "active",
                }
            for subscription_id, subscription in subscription_hosts.items():
                agent = agents.get(subscription["agent_id"])
                if agent:
                    agents[subscription_id] = {
                        **agent,
                        **subscription,
                    }
    except Exception as e:
        logger.warning("Failed to resolve task lookups: %s", e)

    return users, agents, staff, workspaces, requester_ids, author_agent_ids


def _owner_agent_for_task(task, agents: dict | None) -> dict | None:
    """Resolve the accountable deployment without changing execution authorship."""
    subscription_id = getattr(task, "owner_subscription_id", None)
    service_key = getattr(task, "owner_service_key", None)
    if not agents or not (subscription_id or (task.workspace_id and service_key)):
        return None
    candidates = (
        [agents.get(subscription_id)] if subscription_id else agents.values()
    )
    matches = [
        agent for agent in candidates
        if agent
        and agent.get("subscription_id")
        and agent.get("subscription_entity_id") == task.entity_id
        and agent.get("workspace_id") == task.workspace_id
        and (not service_key or agent.get("service_key") == service_key)
        and agent.get("entity_id") in (None, task.entity_id)
        and agent.get("available")
    ]
    return matches[0] if len(matches) == 1 else None


def _current_blocker_for_task(task, latest_log=None) -> dict | None:
    """Project current typed facts, never an error-message interpretation."""
    if task.status != TaskStatus.BLOCKED.value:
        return None
    from packages.core.services.task_execution import task_execution_intent_generation

    details = task.details if isinstance(task.details, dict) else {}
    meta = getattr(latest_log, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    source = details.get("capability_blocker", meta.get("blocker"))
    if source is None and "capability_blocker" not in details and "blocker" not in meta:
        # Legacy logs have no generation fence. Require both the latest recovery
        # event and a lifecycle clock that proves it is not from an earlier wait.
        changed_at = getattr(task, "status_changed_at", None)
        observed_at = getattr(latest_log, "created_at", None)
        if (
            latest_log is None
            or latest_log.log_type != TaskLogType.AI_EXECUTION_FAILED.value
            or meta.get("capability_gap") is not True
            or not isinstance(meta.get("missing_capabilities"), list)
            or not any(isinstance(value, str) and value for value in meta["missing_capabilities"])
            or not changed_at or not observed_at
            or observed_at.replace(tzinfo=observed_at.tzinfo or timezone.utc)
            < changed_at.replace(tzinfo=changed_at.tzinfo or timezone.utc)
        ):
            return None
        source = {
            "kind": "capability_unavailable",
            "stage": "planning",
            "generation": task_execution_intent_generation(task),
            "missing_capabilities": meta.get("missing_capabilities"),
        }
    if (
        not isinstance(source, dict)
        or source.get("generation") != task_execution_intent_generation(task)
        or source.get("kind") not in (
            "capability_unavailable", "tool_missing", "connection_required",
            "approval_required", "policy_denied",
        )
        or source.get("stage") not in ("planning", "execution")
    ):
        return None
    missing = source.get("missing_capabilities")
    missing = [value for value in missing if isinstance(value, str) and value] if isinstance(missing, list) else []
    affected = source.get("affected_agents")
    affected = [
        {key: value for key, value in agent.items() if key in {
            "service_key", "subscription_id", "agent_id", "agent_name",
        } and isinstance(value, str) and value}
        for agent in affected if isinstance(agent, dict)
    ] if isinstance(affected, list) else []
    result = {
        "kind": source["kind"], "stage": source["stage"],
        "generation": source["generation"],
        "missing_capabilities": list(dict.fromkeys(missing)),
        "affected_agents": [agent for agent in affected if agent],
    }
    for key in ("operation_draft_id", "request_id", "log_id", "observed_at"):
        if isinstance(source.get(key), str) and source[key]:
            result[key] = source[key]
    if latest_log is not None:
        result["log_id"] = latest_log.id
        result["observed_at"] = latest_log.created_at.isoformat()
    return result


async def _current_task_blockers(db: AsyncSession, tasks) -> dict[str, dict]:
    """Use current state directly; batch only historical compatibility lookups."""
    blockers = {}
    legacy_tasks = {}
    for task in tasks:
        blocker = _current_blocker_for_task(task)
        if blocker is not None:
            blockers[task.id] = blocker
        elif task.status == TaskStatus.BLOCKED.value and "capability_blocker" not in (task.details or {}):
            legacy_tasks[task.id] = task
    if not legacy_tasks:
        return blockers
    recovery_types = (
        TaskLogType.STATUS_CHANGE.value, TaskLogType.ASSIGNMENT_CHANGE.value,
        TaskLogType.REASSIGN.value, TaskLogType.MANUAL_RETRY.value,
        TaskLogType.AI_NEEDS_REPLAN.value, TaskLogType.AI_EXECUTION_STARTED.value,
        TaskLogType.AI_EXECUTION_COMPLETED.value, TaskLogType.AI_EXECUTION_FAILED.value,
        TaskLogType.AI_HITL_REQUESTED.value, TaskLogType.AI_HITL_RESUMED.value,
        TaskLogType.PLAN_STARTED.value, TaskLogType.PLAN_COMPLETED.value,
        TaskLogType.PLAN_FAILED.value, TaskLogType.PLAN_CANCELLED.value,
        TaskLogType.PLAN_REPLANNED.value,
    )
    ranked = select(
        TaskLog.id,
        func.row_number().over(
            partition_by=TaskLog.task_id,
            order_by=(TaskLog.created_at.desc(), TaskLog.id.desc()),
        ).label("position"),
    ).where(
        TaskLog.task_id.in_(legacy_tasks),
        TaskLog.log_type.in_(recovery_types),
    ).subquery()
    result = await db.execute(
        select(TaskLog).join(ranked, ranked.c.id == TaskLog.id)
        .where(ranked.c.position == 1)
    )
    for log in result.scalars():
        blocker = _current_blocker_for_task(legacy_tasks[log.task_id], log)
        if blocker is not None:
            blockers[log.task_id] = blocker
    return blockers


async def _active_execution_task_ids(db: AsyncSession, tasks) -> set[str]:
    """Return Task IDs whose agent loop is running right now.

    A business Task in ``in_progress`` can be manually moved there before
    its run starts. The board's live indicator must represent the run's
    state, rather than that broad business status.
    """
    task_ids = {task.id for task in tasks if getattr(task, "id", None)}
    if not task_ids:
        return set()
    from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun

    rows = await db.execute(
        select(TaskCoordinatorRun.task_id).where(
            TaskCoordinatorRun.task_id.in_(task_ids),
            TaskCoordinatorRun.phase == TaskCoordinatorPhase.RUNNING.value,
        )
    )
    return {task_id for task_id in rows.scalars() if task_id}


def _to_response(
    t,
    users: dict | None = None,
    agents: dict | None = None,
    staff: dict | None = None,
    workspaces: dict | None = None,
    requester_ids: dict[str, str | None] | None = None,
    author_agent_ids: dict[str, str | None] | None = None,
    *,
    execution_active: bool = False,
    current_blocker: dict | None = None,
) -> TaskResponse:
    """Convert a Task ORM object to TaskResponse with resolved display names.

    Args:
        users: optional {user_id: {name, avatar_url}} lookup
        agents: optional {agent_id: {name, avatar_url}} lookup
    """
    from packages.core.constants.agents import (
        MANOR_AGENT_ID,
        MANOR_AGENT_NAME,
        is_master_agent,
    )

    assignee_name = None
    assignee_avatar = None
    agent_name = None
    agent_avatar = None
    creator_name = None
    creator_avatar = None
    author_agent_name = None
    author_agent_avatar = None
    session_host_agent_id = None
    session_host_subscription_id = None
    session_host_name = None
    session_host_avatar = None
    session_host_role_label = None
    session_host_available = None
    session_host_error = None
    workspace_name = None
    creator_id = (
        requester_ids.get(t.id, t.creator_id)
        if requester_ids is not None
        else t.creator_id
    )
    author_agent_id = (
        author_agent_ids.get(t.id)
        if author_agent_ids is not None
        else None
    )
    owner_agent = _owner_agent_for_task(t, agents) or {}

    if t.workspace_id and workspaces:
        ws = workspaces.get(t.workspace_id)
        if ws:
            workspace_name = ws.get("name")

    if t.assignee_id and is_master_agent(t.assignee_id):
        assignee_name = MANOR_AGENT_NAME
    if t.assignee_id and users:
        u = users.get(t.assignee_id)
        if u:
            assignee_name = u.get("name")
            assignee_avatar = u.get("avatar_url")
    if t.assignee_id and not assignee_name and staff:
        s = staff.get(t.assignee_id)
        if s:
            assignee_name = s.get("name")
            assignee_avatar = s.get("avatar_url")
    if t.assignee_id and not assignee_name and agents:
        a = agents.get(t.assignee_id)
        if a:
            assignee_name = a.get("name")
            assignee_avatar = a.get("avatar_url")

    if creator_id and users:
        cu = users.get(creator_id)
        if cu:
            creator_name = cu.get("name")
            creator_avatar = cu.get("avatar_url")
    if creator_id and not creator_name and staff:
        cs = staff.get(creator_id)
        if cs:
            creator_name = cs.get("name")
            creator_avatar = cs.get("avatar_url")

    if is_master_agent(author_agent_id):
        author_agent_name = MANOR_AGENT_NAME
    elif author_agent_id and agents:
        author = agents.get(author_agent_id)
        if author:
            author_agent_name = author.get("name")
            author_agent_avatar = author.get("avatar_url")

    if is_master_agent(t.agent_id, t.agent_type):
        agent_name = MANOR_AGENT_NAME
    elif t.agent_id and agents:
        a = agents.get(t.agent_id)
        if a:
            agent_name = a.get("name")
            agent_avatar = a.get("avatar_url")

    if t.task_type == TaskType.INTERACTIVE.value:
        session_host_available = False
        owner_subscription_id = getattr(t, "owner_subscription_id", None)
        owner_service_key = getattr(t, "owner_service_key", None)
        if (
            is_master_agent(t.agent_id, t.agent_type)
            and not owner_subscription_id
            and not owner_service_key
        ):
            session_host_agent_id = MANOR_AGENT_ID
            session_host_name = MANOR_AGENT_NAME
            session_host_available = True
        elif owner_subscription_id and agents:
            host = agents.get(owner_subscription_id)
            if (
                host
                and host.get("workspace_id") == t.workspace_id
                and host.get("available")
                and (not t.agent_id or host.get("agent_id") == t.agent_id)
            ):
                session_host_agent_id = host.get("agent_id")
                session_host_subscription_id = owner_subscription_id
                session_host_name = host.get("name")
                session_host_avatar = host.get("avatar_url")
                session_host_role_label = host.get("role_label")
                session_host_available = True
        if not session_host_available:
            session_host_error = "host_unavailable"

    # Avoid triggering async lazy loading from synchronous serialization.
    # ``update_task`` explicitly stamps non-status edits, while list/get
    # queries load the persisted values normally.
    updated_at = getattr(t, "__dict__", {}).get("updated_at")
    status_changed_at = getattr(t, "__dict__", {}).get("status_changed_at")
    actual_output = getattr(t, "actual_output", None)
    if actual_output is not None and not isinstance(actual_output, dict):
        # JSONB accepted scalar outputs before TaskResponse required an object.
        # Preserve legacy text in the current task-runner response field.
        actual_output = (
            {"response": actual_output}
            if isinstance(actual_output, str)
            else {"result": actual_output}
        )

    return TaskResponse(
        id=t.id, entity_id=t.entity_id, title=t.title,
        steered_by_collaborator=bool(getattr(t, "collaborator_ids", None)),
        description=t.description, status=t.status, priority=t.priority,
        task_type=t.task_type, workspace_id=t.workspace_id,
        workspace_name=workspace_name,
        category_id=t.category_id, assignee_id=t.assignee_id,
        agent_id=t.agent_id, agent_type=t.agent_type,
        owner_service_key=getattr(t, "owner_service_key", None),
        owner_subscription_id=getattr(t, "owner_subscription_id", None),
        owner_agent_id=owner_agent.get("agent_id"),
        owner_agent_name=owner_agent.get("name"),
        owner_agent_avatar=owner_agent.get("avatar_url"),
        current_blocker=current_blocker or _current_blocker_for_task(t),
        session_host_agent_id=session_host_agent_id,
        session_host_subscription_id=session_host_subscription_id,
        session_host_name=session_host_name,
        session_host_avatar=session_host_avatar,
        session_host_role_label=session_host_role_label,
        session_host_available=session_host_available,
        session_host_error=session_host_error,
        creator_id=creator_id,
        conversation_id=t.conversation_id,
        parent_task_id=getattr(t, "parent_task_id", None),
        required_skills=list(getattr(t, "required_skills", []) or []),
        estimated_hours=getattr(t, "estimated_hours", None),
        visibility=getattr(t, "visibility", "entity") or "entity",
        owner_id=getattr(t, "owner_id", None),
        client_visible=bool(getattr(t, "client_visible", False)),
        sla_policy_id=getattr(t, "sla_policy_id", None),
        sla_breached=bool(getattr(t, "sla_breached", False)),
        escalation_level=int(getattr(t, "escalation_level", 0) or 0),
        details=t.details or {},
        actual_output=actual_output,
        deadline=t.deadline.isoformat() if t.deadline else None,
        scheduled_at=(t.details or {}).get("scheduled_at"),
        duration_minutes=(t.details or {}).get("duration_minutes"),
        started_at=t.started_at.isoformat() if t.started_at else None,
        completed_at=t.completed_at.isoformat() if t.completed_at else None,
        created_at=t.created_at.isoformat() if t.created_at else None,
        updated_at=updated_at.isoformat() if updated_at else None,
        status_changed_at=(
            status_changed_at.isoformat() if status_changed_at else None
        ),
        execution_active=execution_active,
        assignee_name=assignee_name, assignee_avatar=assignee_avatar,
        agent_name=agent_name, agent_avatar=agent_avatar,
        creator_name=creator_name, creator_avatar=creator_avatar,
        author_agent_id=author_agent_id,
        author_agent_name=author_agent_name,
        author_agent_avatar=author_agent_avatar,
    )


# ── Category Endpoints (fixed paths — before /{task_id}) ──

@router.get("/categories", response_model=list[CategoryResponse])
async def list_task_categories(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cats = await list_categories(db, user.entity_id)
    return [
        CategoryResponse(
            id=c.id, entity_id=c.entity_id, name=c.name,
            icon=c.icon, color=c.color, sort_order=c.sort_order,
        )
        for c in cats
    ]


@router.post("/categories", response_model=CategoryResponse, status_code=201)
async def create_task_category(
    req: CategoryCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cat = await create_category(
        db, user.entity_id, name=req.name,
        icon=req.icon, color=req.color, sort_order=req.sort_order,
    )
    return CategoryResponse(
        id=cat.id, entity_id=cat.entity_id, name=cat.name,
        icon=cat.icon, color=cat.color, sort_order=cat.sort_order,
    )


@router.put("/categories/{category_id}", response_model=CategoryResponse)
async def update_task_category(
    category_id: str,
    req: CategoryUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cat = await update_category(db, category_id, user.entity_id, **req.model_dump(exclude_none=True))
    if not cat:
        raise HTTPException(404, "Category not found")
    return CategoryResponse(
        id=cat.id, entity_id=cat.entity_id, name=cat.name,
        icon=cat.icon, color=cat.color, sort_order=cat.sort_order,
    )


@router.delete("/categories/{category_id}", status_code=204)
async def delete_task_category(
    category_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_category(db, category_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Category not found")


# ── SLA Policies ──
#
# Backs the SLA picker on Task Detail and the read-only SLA chip in the
# header. Soft-delete (status='inactive') so existing tasks pointing at
# a removed policy don't 404 the detail page.

class SlaPolicyResponse(BaseModel):
    id: str
    name: str
    response_seconds: int
    resolution_seconds: int
    priority: str | None = None
    category_id: str | None = None
    status: str


class SlaPolicyCreateRequest(BaseModel):
    name: str
    response_seconds: int = 3600
    resolution_seconds: int = 86400
    priority: str | None = None
    category_id: str | None = None


class SlaPolicyUpdateRequest(BaseModel):
    name: str | None = None
    response_seconds: int | None = None
    resolution_seconds: int | None = None
    priority: str | None = None
    category_id: str | None = None
    status: str | None = None


def _to_sla_response(p) -> SlaPolicyResponse:
    return SlaPolicyResponse(
        id=p.id, name=p.name,
        response_seconds=p.response_seconds,
        resolution_seconds=p.resolution_seconds,
        priority=p.priority, category_id=p.category_id,
        status=p.status,
    )


@router.get("/sla-policies", response_model=list[SlaPolicyResponse])
async def list_my_sla_policies(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    policies = await list_sla_policies(db, user.entity_id)
    return [_to_sla_response(p) for p in policies]


@router.post("/sla-policies", response_model=SlaPolicyResponse, status_code=201)
async def create_my_sla_policy(
    req: SlaPolicyCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await create_sla_policy(
        db, user.entity_id,
        name=req.name,
        response_seconds=req.response_seconds,
        resolution_seconds=req.resolution_seconds,
        priority=req.priority, category_id=req.category_id,
    )
    return _to_sla_response(p)


@router.put("/sla-policies/{policy_id}", response_model=SlaPolicyResponse)
async def update_my_sla_policy(
    policy_id: str,
    req: SlaPolicyUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await update_sla_policy(
        db, policy_id, user.entity_id,
        **{k: v for k, v in req.dict().items() if v is not None},
    )
    if not p:
        raise HTTPException(404, "SLA policy not found")
    return _to_sla_response(p)


@router.delete("/sla-policies/{policy_id}", status_code=204)
async def delete_my_sla_policy(
    policy_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_sla_policy(db, policy_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "SLA policy not found")


# ── Task Endpoints ──

@router.get("", response_model=TaskListResponse)
async def list_my_tasks(
    status: str | None = Query(None),
    workspace_id: str | None = Query(None),
    workspace_id_alias: str | None = Query(None, alias="workspaceId"),
    category_id: str | None = Query(None),
    parent_task_id: str | None = Query(None, description="Filter to direct children of this task — used by the Subtasks panel on TaskDetail"),
    attention: bool = Query(False, description="Return only tasks whose current status needs user attention, ordered by the latest status transition"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    effective_workspace_id = workspace_id or workspace_id_alias
    await require_workspace_readable(db, user, effective_workspace_id)
    from packages.core.services.workspace_access import readable_workspace_ids_for_user
    readable_ws = await readable_workspace_ids_for_user(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    tasks, total = await list_tasks(
        db, user.entity_id,
        status=status, workspace_id=effective_workspace_id,
        category_id=category_id,
        parent_task_id=parent_task_id,
        attention_only=attention,
        limit=limit, offset=offset,
        readable_workspace_ids=readable_ws,
    )
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, tasks)
    )
    active_execution_ids = await _active_execution_task_ids(db, tasks)
    blockers = await _current_task_blockers(db, tasks)
    return TaskListResponse(
        items=[
            _to_response(
                t,
                users,
                agents,
                staff,
                workspaces,
                requester_ids,
                author_agent_ids,
                execution_active=t.id in active_execution_ids,
                current_blocker=blockers.get(t.id),
            )
            for t in tasks
        ],
        total=total,
    )


@router.post("", response_model=TaskResponse, status_code=201)
async def create_new_task(
    req: TaskCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_writable(db, user, req.workspace_id)
    if req.task_type == TaskType.INTERACTIVE.value:
        from packages.core.constants.agents import is_master_agent

        if not req.workspace_id:
            raise HTTPException(422, "Interactive Tasks must belong to a Workspace")
        if not (
            req.agent_id
            or req.owner_service_key
            or req.owner_subscription_id
            or is_master_agent(req.agent_id, req.agent_type)
        ):
            raise HTTPException(422, "Interactive Tasks require a Host Agent")
    try:
        await ensure_workspace_agent_assignment(
            db,
            entity_id=user.entity_id,
            workspace_id=req.workspace_id,
            agent_id=req.agent_id,
        )
        await ensure_workspace_owner_subscription(
            db,
            entity_id=user.entity_id,
            workspace_id=req.workspace_id,
            subscription_id=req.owner_subscription_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    required_skills = await _required_skill_refs(db, user, req.required_skills or [])
    from packages.core.services.task_session import TaskSessionHostError

    try:
        task = await create_task(
            db, user.entity_id,
            title=req.title, description=req.description,
            priority=req.priority, task_type=req.task_type,
            workspace_id=req.workspace_id, category_id=req.category_id,
            assignee_id=req.assignee_id, agent_id=req.agent_id,
            agent_type=req.agent_type, creator_id=user.id,
            owner_service_key=req.owner_service_key,
            owner_subscription_id=req.owner_subscription_id,
            required_skills=required_skills,
            details=_without_task_runtime_details(req.details), deadline=req.deadline,
            scheduled_at=req.scheduled_at, duration_minutes=req.duration_minutes,
        )
    except TaskSessionHostError as exc:
        raise HTTPException(409, str(exc)) from exc

    # Explicit AI execution intent can name either a legacy Agent or a
    # Workspace service deployment. A naked Workspace Task stays pending even
    # if Workspace defaults later populate its runtime requirements; the
    # closed factory decides which explicit intent is actually dispatchable.
    from packages.core.constants.agents import is_master_agent
    has_explicit_execution_intent = bool(
        req.agent_id
        or req.owner_service_key
        or req.owner_subscription_id
        or is_master_agent(req.agent_id, req.agent_type)
    )
    if has_explicit_execution_intent:
        try:
            from packages.core.services.task_execution import queue_initial_task_execution

            await queue_initial_task_execution(db, task, user_id=user.id)
        except Exception as e:
            logger.warning("Failed to dispatch agent task: %s", e)

    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return _to_response(
        task, users, agents, staff, workspaces, requester_ids, author_agent_ids,
    )


@router.post("/from-template", response_model=TaskResponse, status_code=201)
async def create_from_template(
    req: TaskFromTemplateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new task pre-filled from a task template."""
    overrides = req.model_dump(exclude={"template_id"}, exclude_none=True)
    await require_workspace_writable(db, user, overrides.get("workspace_id"))
    try:
        await ensure_workspace_agent_assignment(
            db,
            entity_id=user.entity_id,
            workspace_id=overrides.get("workspace_id"),
            agent_id=overrides.get("agent_id"),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    from packages.core.services.template_service import instantiate_template
    try:
        task = await instantiate_template(
            db, user.entity_id, req.template_id,
            variables=overrides, creator_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return _to_response(
        task, users, agents, staff, workspaces, requester_ids, author_agent_ids,
    )


def _normalize_task_board_columns(
    value: object,
    *,
    append_missing: bool,
) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    if isinstance(value, list):
        for key in value:
            if key in _TASK_BOARD_COLUMNS and key not in seen:
                seen.add(key)
                normalized.append(key)
    if append_missing:
        normalized.extend(key for key in _TASK_BOARD_COLUMNS if key not in seen)
    return normalized


def _task_board_preferences_response(
    value: object,
    *,
    configured: bool,
) -> TaskBoardPreferencesResponse:
    preferences = value if isinstance(value, dict) else {}
    mode = preferences.get("mode")
    visible_columns = _normalize_task_board_columns(
        preferences.get("visible_columns"),
        append_missing=False,
    )
    return TaskBoardPreferencesResponse(
        mode=mode if mode in {"kanban", "list"} else "kanban",
        visible_columns=visible_columns or list(_TASK_BOARD_COLUMNS),
        column_order=_normalize_task_board_columns(
            preferences.get("column_order"),
            append_missing=True,
        ),
        configured=configured,
    )


@router.get("/board-preferences", response_model=TaskBoardPreferencesResponse)
async def get_task_board_preferences(
    user: User = Depends(get_current_user),
):
    """Get the current user's cross-device task board preferences."""
    preferences = user.preferences or {}
    stored = preferences.get(_TASK_BOARD_PREFERENCES_KEY)
    return _task_board_preferences_response(
        stored,
        configured=isinstance(stored, dict),
    )


@router.put("/board-preferences", response_model=TaskBoardPreferencesResponse)
async def put_task_board_preferences(
    req: TaskBoardPreferencesRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist task board preferences on the authenticated user."""
    normalized = _task_board_preferences_response(
        req.model_dump(),
        configured=True,
    )
    stored = normalized.model_dump(exclude={"configured"})
    await update_user_preferences(
        db,
        user.id,
        {_TASK_BOARD_PREFERENCES_KEY: stored},
    )
    return normalized


@router.get("/board", response_model=dict)
async def task_board(
    workspace_id: str | None = Query(None),
    workspace_id_alias: str | None = Query(None, alias="workspaceId"),
    limit_per_status: int = Query(50, ge=1, le=200),
    offset_per_status: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tasks grouped by status for Kanban board view."""
    effective_workspace_id = workspace_id or workspace_id_alias
    await require_workspace_readable(db, user, effective_workspace_id)
    from packages.core.services.workspace_access import readable_workspace_ids_for_user
    readable_ws = await readable_workspace_ids_for_user(
        db, entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    board = await get_tasks_by_status(
        db, user.entity_id, workspace_id=effective_workspace_id,
        readable_workspace_ids=readable_ws,
        limit_per_status=limit_per_status,
        offset_per_status=offset_per_status,
    )
    counts = board.pop("_counts", {})
    all_tasks = [t for tasks in board.values() for t in tasks]
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, all_tasks)
    )
    active_execution_ids = await _active_execution_task_ids(db, all_tasks)
    blockers = await _current_task_blockers(db, all_tasks)
    result = {
        status: [
            _to_response(
                t,
                users,
                agents,
                staff,
                workspaces,
                requester_ids,
                author_agent_ids,
                execution_active=t.id in active_execution_ids,
                current_blocker=blockers.get(t.id),
            )
            for t in tasks
        ]
        for status, tasks in board.items()
    }
    result["_counts"] = counts
    return result


@router.post("/{task_id}/move", response_model=TaskResponse)
async def move_task_endpoint(
    task_id: str,
    req: MoveTaskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Move a task to a different status column (Kanban drag-and-drop)."""
    existing = await get_task(db, task_id, user.entity_id)
    if not existing:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, existing.workspace_id)
    try:
        task = await move_task(db, task_id, user.entity_id, req.status)
    except TaskStatusTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "Task not found")
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return _to_response(
        task, users, agents, staff, workspaces, requester_ids, author_agent_ids,
    )


@router.post("/{task_id}/retry", response_model=RetryTaskResponse)
async def retry_task_endpoint(
    task_id: str,
    req: RetryTaskRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually retry a failed/blocked/HITL task.

    A Task's run (its agent loop) restarts.
    """
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, task.workspace_id)
    from packages.core.services.task_retry_service import (
        TaskRetryError,
        dispatch_task_retry,
        mark_task_retry_dispatch_failed,
        prepare_task_retry,
    )

    try:
        retry_result = await prepare_task_retry(
            db,
            task=task,
            user_id=user.id,
            user_label=user.display_name or user.email,
            note=req.note if req else None,
        )
    except TaskRetryError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    task = retry_result.task

    from packages.core.services.task_chat_hitl import resolve_task_hitl

    await resolve_task_hitl(
        db,
        task=task,
        kind=PendingActionKind.TASK_RECOVERY,
        choice="retry",
        user_id=user.id,
        note=req.note if req else None,
    )
    await db.commit()
    await db.refresh(task)

    dispatched = False
    try:
        dispatched = dispatch_task_retry(retry_result)
    except Exception as exc:
        logger.warning(
            "Task retry dispatch failed: task=%s mode=%s error=%s",
            task.id,
            retry_result.mode,
            exc,
        )
        try:
            await mark_task_retry_dispatch_failed(
                db,
                result=retry_result,
                error=exc,
            )
            await db.commit()
            await db.refresh(task)
        except Exception:
            await db.rollback()
            logger.error(
                "Could not persist Task retry dispatch failure: task=%s",
                task.id,
                exc_info=True,
            )

    from packages.core.services.realtime import broadcast_task_runtime_update

    await broadcast_task_runtime_update(
        task.entity_id,
        task_id=task.id,
        workspace_id=task.workspace_id,
        event="task_retry_resolved",
    )

    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return RetryTaskResponse(
        task=_to_response(
            task,
            users,
            agents,
            staff,
            workspaces,
            requester_ids,
            author_agent_ids,
        ),
        dispatched=dispatched,
        mode=retry_result.mode,
    )


async def _resume_hitl(
    db: AsyncSession,
    task,
    user: User,
    *,
    response_text: str,
    payload: dict,
) -> dict:
    """Resume a task waiting on structured human input.

    A Task agent loop parked on its question takes the answer. The comment
    path and a Task's question card outside Workspace Chat call this too.
    """
    submitted_by = user.display_name or user.email

    # Re-read and lock the Task before entering the runtime. The closed
    # factory owns this decision; assignment fields are not a second routing
    # switch. The per-Task approval boundary comes first: a terminal transition takes
    # it before the input request and the Task, so an answer serializes with
    # it (and with a Task agent loop's park) instead of locking in reverse.
    from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

    await lock_task_runtime_approval_boundary(db, task_id=task.id)
    locked_task = (await db.execute(
        select(Task).where(
            Task.id == task.id,
            Task.entity_id == task.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked_task is None:
        return {
            "resumed": False,
            "dispatched": False,
            "mode": None,
            "detail": "This Task is no longer available for input resume.",
        }
    task = locked_task

    from packages.core.services.task_agent_loop_hitl import task_agent_loop_waiting_on_input

    if (
        # A comment or the Task page answers only a Task that waits on its
        # person. While the Task is in progress (a delegated agent asked
        # while others still work) it stays a plain comment; that agent's
        # card names its request and answers it at any time.
        task.status == TaskStatus.WAITING_ON_CUSTOMER
        and await task_agent_loop_waiting_on_input(db, task)
    ):
        # A Task agent loop parked on its question: the shared answer service
        # writes the answer into its checkpoint and stages its wake.
        from packages.core.services.task_input_resume import apply_task_input_answer

        applied = await apply_task_input_answer(
            db,
            task=task,
            user_id=user.id,
            user_label=submitted_by,
            response_text=response_text,
            payload=payload,
        )
        if applied["resumed"]:
            await db.commit()
            # The wake was committed with the answer; the coordinator outbox
            # delivers it (there is no separate post-commit hint for it).
            from packages.core.services.realtime import broadcast_task_runtime_update

            await broadcast_task_runtime_update(
                task.entity_id,
                task_id=task.id,
                workspace_id=task.workspace_id,
                event="task_hitl_resolved",
            )
            return {
                "resumed": True,
                "dispatched": True,
                "mode": "agent_loop",
            }
        return {
            "resumed": False,
            "dispatched": False,
            "mode": None,
            "detail": "This Task is no longer waiting for an answer.",
        }

    # Every Task run is an agent loop: one not parked on a question above has
    # nothing to resume.
    return {
        "resumed": False,
        "dispatched": False,
        "mode": None,
        "detail": "This Task is not waiting for an answer.",
    }


@router.post("/{task_id}/hitl-response", response_model=HITLResponseResponse)
async def respond_to_hitl(
    task_id: str,
    req: HITLResponseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit structured human input and resume a waiting task."""
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, task.workspace_id)

    response_text = (req.response or req.note or "").strip()
    fields = req.fields or {}
    if not response_text and not req.choice and not fields:
        raise HTTPException(400, "response, choice, or fields is required")

    payload = {
        "response": response_text,
        "choice": req.choice,
        "fields": fields,
        "note": req.note,
        "request_id": req.request_id,
    }
    result = await _resume_hitl(
        db,
        task,
        user,
        response_text=response_text,
        payload=payload,
    )
    if not result["resumed"]:
        raise HTTPException(
            409,
            result.get("detail") or "Task is not waiting for structured human input",
        )

    queued_learning_ids = await _record_task_user_decision_evidence(
        db,
        user=user,
        task=task,
        evidence_type="hitl_resolution",
        summary=(
            f"Task HITL response submitted for '{task.title}'"
            + (f": {response_text[:240]}" if response_text else "")
        ),
        details={
            "choice": req.choice,
            "response": response_text,
            "field_keys": sorted(str(key) for key in fields.keys())[:30],
            "resume_result": result,
        },
        metrics={"field_count": len(fields)},
    )
    await db.commit()
    await _enqueue_learning_candidate_applies(
        db,
        user=user,
        workspace_id=task.workspace_id,
        candidate_ids=queued_learning_ids,
    )
    await db.flush()
    await db.refresh(task)
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return HITLResponseResponse(
        task=_to_response(
            task,
            users,
            agents,
            staff,
            workspaces,
            requester_ids,
            author_agent_ids,
        ),
        **result,
    )


@router.post("/{task_id}/approval", response_model=TaskResponse)
async def decide_approval_task(
    task_id: str,
    req: ApprovalDecisionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record a user's decision on an approval task and notify workspace AI."""
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_authority(
        db,
        user,
        task.workspace_id,
        "approve_tasks",
    )
    actor = user.display_name or user.email
    from packages.core.services.task_approval_service import (
        TaskApprovalDecisionError,
        apply_task_approval_decision,
    )

    try:
        result = await apply_task_approval_decision(
            db,
            task=task,
            user_id=user.id,
            actor=actor,
            choice=req.choice,
            note=req.note,
        )
    except TaskApprovalDecisionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    updated = result.task
    log = result.log
    decision = result.decision
    choice = result.choice
    approved = result.approved
    note = result.note

    from packages.core.services.task_chat_hitl import resolve_task_hitl

    await resolve_task_hitl(
        db,
        task=updated,
        kind=PendingActionKind.TASK_APPROVAL,
        choice=choice,
        user_id=user.id,
        note=note,
    )
    if updated.workspace_id:
        try:
            from packages.core.services.workspace_service import record_activity

            await record_activity(
                db,
                updated.workspace_id,
                user.entity_id,
                event_type="task.approval_decision",
                summary=(
                    f"{actor} approved task '{updated.title}'"
                    if approved else
                    f"{actor} requested changes for task '{updated.title}'"
                ),
                details={
                    "task_id": updated.id,
                    "task_log_id": log.id,
                    "decision": decision,
                    "choice": choice,
                    "approved": approved,
                    "note_preview": note[:240],
                },
                user_id=user.id,
                agent_id=updated.agent_id,
            )
        except Exception:
            logger.debug("approval decision workspace activity skipped", exc_info=True)
    await db.flush()
    await db.refresh(updated)

    queued_learning_ids = await _record_task_user_decision_evidence(
        db,
        user=user,
        task=updated,
        evidence_type="approval_decision",
        summary=(
            f"Approval task {decision.replace('_', ' ')} for '{updated.title}'"
            + (f": {note[:240]}" if note else "")
        ),
        details={
            "decision": decision,
            "choice": choice,
            "approved": approved,
            "note": note,
            "log_id": log.id,
        },
        metrics={"approved": 1 if approved else 0},
    )
    await db.commit()
    from packages.core.services.realtime import broadcast_task_runtime_update

    await broadcast_task_runtime_update(
        updated.entity_id,
        task_id=updated.id,
        workspace_id=updated.workspace_id,
        event="task_approval_resolved",
    )
    await _enqueue_learning_candidate_applies(
        db,
        user=user,
        workspace_id=updated.workspace_id,
        candidate_ids=queued_learning_ids,
    )
    if updated.workspace_id:
        _schedule_workspace_task_comment_processing(
            task_id=updated.id,
            entity_id=user.entity_id,
            user_id=user.id,
            author_label=actor,
            comment=(
                f"Approval decision for task '{updated.title}': {decision}."
                + (f" Note: {note}" if note else "")
            ),
            log_id=log.id,
        )

    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [updated])
    )
    return _to_response(
        updated, users, agents, staff, workspaces, requester_ids, author_agent_ids,
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_one_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_readable(db, user, task.workspace_id)
    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    blockers = await _current_task_blockers(db, [task])
    response = _to_response(
        task, users, agents, staff, workspaces, requester_ids, author_agent_ids,
        current_blocker=blockers.get(task.id),
    )
    return response


@router.delete("/{task_id}", status_code=204)
async def delete_one_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, task.workspace_id)
    authorized_workspace_id = task.workspace_id

    if authorized_workspace_id:
        from packages.core.services.chat_feedback import (
            lock_completion_feedback_subject,
        )

        await lock_completion_feedback_subject(db, task_id=task_id)

    # Serialize every Task deletion with thread creation. The interactive
    # cleanup service takes the same lock again before reading Conversations;
    # this route-level lock also covers ordinary Task threads.
    task = (await db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.entity_id == user.entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")
    if task.workspace_id != authorized_workspace_id:
        await require_workspace_writable(db, user, task.workspace_id)

    from packages.core.services.task_session import delete_task_session_conversations

    await delete_task_session_conversations(db, task)
    if task.workspace_id:
        from packages.core.services.chat_feedback import (
            delete_task_completion_feedback,
        )

        await delete_task_completion_feedback(
            db,
            entity_id=user.entity_id,
            workspace_id=task.workspace_id,
            task_id=task.id,
        )
    await db.delete(task)
    await db.flush()


@router.put("/{task_id}", response_model=TaskResponse)
async def update_one_task(
    task_id: str,
    req: TaskUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Capture old agent_id before update
    old_task = await get_task(db, task_id, user.entity_id)
    if old_task:
        # Gate on the task's CURRENT workspace, and on the target workspace
        # when the update moves the task into a different one.
        await require_workspace_writable(db, user, old_task.workspace_id)
        new_ws = getattr(req, "workspace_id", None)
        if new_ws and new_ws != old_task.workspace_id:
            await require_workspace_writable(db, user, new_ws)
    old_agent_id = old_task.agent_id if old_task else None
    old_agent_type = old_task.agent_type if old_task else None
    old_owner_service_key = old_task.owner_service_key if old_task else None
    old_owner_subscription_id = (
        old_task.owner_subscription_id if old_task else None
    )
    old_status = old_task.status if old_task else None

    requested_agent_id = str(req.agent_id or "").strip()
    if requested_agent_id:
        try:
            await ensure_workspace_agent_assignment(
                db,
                entity_id=user.entity_id,
                workspace_id=old_task.workspace_id if old_task else None,
                agent_id=requested_agent_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if req.owner_subscription_id:
        try:
            await ensure_workspace_owner_subscription(
                db,
                entity_id=user.entity_id,
                workspace_id=getattr(req, "workspace_id", None) or (old_task.workspace_id if old_task else None),
                subscription_id=req.owner_subscription_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    # Build update payload — empty strings mean "clear this field"
    update_data = {}
    for key, val in req.model_dump(exclude_none=True).items():
        if key in ("scheduled_at", "duration_minutes"):
            continue  # handled below via details merge
        if isinstance(val, str) and val == "" and key in (
            "assignee_id",
            "agent_id",
            "agent_type",
            "category_id",
            "owner_service_key",
            "owner_subscription_id",
        ):
            update_data[key] = None
        else:
            update_data[key] = val
    if "required_skills" in update_data:
        update_data["required_skills"] = await _required_skill_refs(db, user, update_data["required_skills"])
    if "details" in update_data:
        update_data["details"] = _merge_task_details(
            old_task.details if old_task else None,
            _without_task_runtime_details(update_data.get("details")),
        )
    # Merge scheduled_at/duration_minutes into details JSONB
    if req.scheduled_at is not None or req.duration_minutes is not None:
        existing_details = _merge_task_details(
            old_task.details if old_task else None,
            update_data.get("details"),
        )
        if req.scheduled_at is not None:
            existing_details["scheduled_at"] = req.scheduled_at or None
        if req.duration_minutes is not None:
            existing_details["duration_minutes"] = req.duration_minutes or None
        update_data["details"] = existing_details
    from packages.core.services.task_session import TaskSessionHostError

    try:
        task = await update_task(
            db, task_id, user.entity_id, user_id=user.id, manual_execution=True, **update_data,
        )
    except (TaskStatusTransitionError, TaskSessionHostError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "Task not found")
    if (
        getattr(user, "_collaborator_workspace_ids", None) is None
        and task.collaborator_ids
        and _TASK_CONTENT_FIELDS & set(update_data)
    ):
        # A customer member saving the Task's content takes ownership of it:
        # its runs are no longer narrowed to the collaborators who steered it.
        task.collaborator_ids = None
    # Any explicit execution-identity change enters the closed factory. The
    # factory, not this API, decides whether that Task type/runtime starts.
    from packages.core.constants.agents import is_master_agent as _is_master
    new_agent_id = task.agent_id
    new_agent_type = task.agent_type
    was_master = _is_master(old_agent_id, old_agent_type)
    now_master = _is_master(new_agent_id, new_agent_type)
    execution_identity_changed = bool(
        ("agent_id" in update_data and new_agent_id != old_agent_id)
        or ("agent_type" in update_data and now_master != was_master)
        or (
            "owner_service_key" in update_data
            and task.owner_service_key != old_owner_service_key
        )
        or (
            "owner_subscription_id" in update_data
            and task.owner_subscription_id != old_owner_subscription_id
        )
    )
    has_execution_identity = bool(
        new_agent_id
        or task.owner_service_key
        or task.owner_subscription_id
        or now_master
    )
    if (
        execution_identity_changed
        and has_execution_identity
        and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED,)
    ):
        try:
            from packages.core.services.task_execution import queue_initial_task_execution

            await queue_initial_task_execution(db, task, user_id=user.id)
        except Exception as e:
            logger.warning("Failed to dispatch agent task: %s", e)

    # HITL resumption is an explicit execution intent. Runtime selection and
    # dispatchability come exclusively from the factory.
    new_status = update_data.get("status")
    if (
        old_status == "waiting_on_customer"
        and new_status in ("pending", "in_progress")
    ):
        try:
            from packages.core.services.task_execution import queue_initial_task_execution

            await queue_initial_task_execution(db, task, user_id=user.id)
        except Exception as e:
            logger.warning("HITL resumption failed: %s", e)

    if (
        old_status
        and new_status
        and new_status != old_status
    ):
        await _record_task_user_decision_evidence(
            db,
            user=user,
            task=task,
            evidence_type="task_status_change",
            summary=f"Task status changed for '{task.title}': {old_status} -> {new_status}",
            details={
                "old_status": old_status,
                "new_status": new_status,
                "changed_from": "task_detail",
            },
        )
        if task.workspace_id:
            from packages.core.services.workspace_service import record_activity

            await record_activity(
                db,
                task.workspace_id,
                user.entity_id,
                event_type="task.status_changed",
                summary=f"Task status changed for '{task.title}': {old_status} -> {new_status}",
                details={
                    "task_id": task.id,
                    "old_status": old_status,
                    "new_status": new_status,
                },
                user_id=user.id,
                agent_id=task.agent_id,
                dispatch_triggers=False,
            )
        await db.flush()

    users, agents, staff, workspaces, requester_ids, author_agent_ids = (
        await _resolve_lookups(db, [task])
    )
    return _to_response(
        task, users, agents, staff, workspaces, requester_ids, author_agent_ids,
    )


@router.get("/{task_id}/history")
async def get_task_history(
    task_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get field-level change history for a task."""
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_readable(db, user, task.workspace_id)
    from packages.core.services.change_tracker import get_change_history
    history = await get_change_history(db, user.entity_id, "task", task_id, limit=limit)
    return history


@router.get("/{task_id}/logs", response_model=list[TaskLogResponse])
async def get_logs(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify task belongs to entity
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_readable(db, user, task.workspace_id)
    logs = await get_task_logs(db, task_id)
    return [
        TaskLogResponse(
            id=log.id, task_id=log.task_id, log_type=log.log_type,
            content=log.content, created_by=log.created_by,
            created_at=log.created_at.isoformat() if log.created_at else None,
            attachments=(log.meta or {}).get("attachments", []),
            author_agent_id=(log.meta or {}).get("agent_id"),
            author_agent_name=(log.meta or {}).get("agent_name"),
            meta=log.meta,
        )
        for log in logs
    ]


@router.post("/{task_id}/logs", response_model=TaskLogResponse, status_code=201)
async def add_log(
    task_id: str,
    req: AddLogRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, task.workspace_id)
    meta = {}
    if req.attachments:
        meta["attachments"] = req.attachments
    mention_agent_items: list[dict] = []
    mention_user_items: list[dict] = []
    if req.log_type == TaskLogType.COMMENT and req.mentions:
        mention_agent_items, mention_user_items = await validate_mentions(
            db, entity_id=user.entity_id, raw=req.mentions,
        )
        if mention_agent_items or mention_user_items:
            meta["mentions"] = mention_agent_items + mention_user_items
    if (
        (task.status in (TaskStatus.WAITING_ON_CUSTOMER, TaskStatus.IN_PROGRESS,) or req.request_id)
        and req.log_type == TaskLogType.COMMENT
    ):
        # This comment may answer a waiting Task (_resume_hitl below). Take the
        # per-Task approval boundary before the log row references the Task,
        # in the terminal path's boundary -> request -> Task order.
        from packages.core.services.task_runtime_approvals import lock_task_runtime_approval_boundary

        await lock_task_runtime_approval_boundary(db, task_id=task.id)
    log = await add_task_log(
        db, task_id, req.log_type, req.content,
        actor=TaskActor.USER,
        created_by=(user.display_name or user.email),
        metadata=meta if meta else None,
    )

    hitl_result = {"resumed": False}
    queued_learning_ids: list[str] = []
    # HITL resumption: comment on a waiting task resumes execution
    if (task.status in (TaskStatus.WAITING_ON_CUSTOMER, TaskStatus.IN_PROGRESS,) or req.request_id) and req.log_type == TaskLogType.COMMENT:
        payload = {"response": req.content, "fields": {}, "source": "comment", "request_id": req.request_id,
                   "attachments": req.attachments}
        hitl_result = await _resume_hitl(db, task, user, response_text=req.content.strip(), payload=payload)
        if hitl_result.get("resumed"):
            queued_learning_ids.extend(await _record_task_user_decision_evidence(
                db,
                user=user,
                task=task,
                evidence_type="hitl_resolution",
                summary=(
                    f"Task comment resumed HITL for '{task.title}'"
                    + (f": {req.content[:240]}" if req.content else "")
                ),
                details={
                    "choice": "comment",
                    "response": req.content,
                    "resume_result": hitl_result,
                    "task_log_id": log.id,
                },
            ))

    if (
        req.log_type == TaskLogType.COMMENT
        and bool((req.content or "").strip())
        and not _is_attachment_only_comment(req.content, req.attachments)
    ):
        queued_learning_ids.extend(await _record_task_user_decision_evidence(
            db,
            user=user,
            task=task,
            evidence_type="task_comment",
            summary=f"User commented on task '{task.title}': {req.content[:240]}",
            details={
                "comment": req.content,
                "task_log_id": log.id,
                "resumed_hitl": bool(hitl_result.get("resumed")),
            },
            metrics={"comment_chars": len(req.content or "")},
        ))
        if task.workspace_id:
            try:
                from packages.core.services.workspace_service import record_activity

                await record_activity(
                    db,
                    task.workspace_id,
                    user.entity_id,
                    event_type="task.comment",
                    summary=f"User commented on task '{task.title}': {req.content[:160]}",
                    details={
                        "task_id": task.id,
                        "task_log_id": log.id,
                        "resumed_hitl": bool(hitl_result.get("resumed")),
                        "comment_preview": req.content[:240],
                    },
                    user_id=user.id,
                    agent_id=task.agent_id,
                )
            except Exception:
                logger.debug("task comment workspace activity skipped", exc_info=True)

    # ── Which agents respond? ────────────────────────────────────────
    # Agent/workspace-owned tasks keep the existing auto-reply; tasks
    # assigned to a human (or unassigned) only run agents that were
    # explicitly @mentioned. Mentions always stack on top, deduped.
    has_agent_owner = bool(
        task.agent_id or task.owner_subscription_id or task.owner_service_key
    )
    responder_agent_ids: list = []
    if has_agent_owner:
        responder_agent_ids.append(task.agent_id)  # None → workspace default agent
        # Edge (v1 known): the None sentinel can't be deduped against a concrete agent id,
        # so a mention targeting the workspace default may dispatch that agent twice.
    for item in mention_agent_items:
        if item["id"] not in responder_agent_ids:
            responder_agent_ids.append(item["id"])

    should_process_workspace_comment = (
        req.log_type == TaskLogType.COMMENT
        and bool((req.content or "").strip())
        and bool(task.workspace_id)
        and not bool(hitl_result.get("resumed"))
        and not bool(hitl_result.get("handled"))
        and not _is_attachment_only_comment(req.content, req.attachments)
        and bool(responder_agent_ids)
    )
    if should_process_workspace_comment or queued_learning_ids or mention_user_items:
        await db.commit()
        await _enqueue_learning_candidate_applies(
            db,
            user=user,
            workspace_id=task.workspace_id,
            candidate_ids=queued_learning_ids,
        )
    if should_process_workspace_comment:
        _schedule_workspace_task_comment_processing(
            responder_agent_ids=responder_agent_ids,
            task_id=task.id,
            entity_id=user.entity_id,
            user_id=user.id,
            author_label=user.display_name or user.email,
            comment=req.content,
            log_id=log.id,
        )
    # ── Staff mentions: notify via gateway, channels per user prefs. ──
    # Independent of assignee type and HITL state.
    if mention_user_items:
        _schedule_background(notify_mentioned_users(
            entity_id=user.entity_id,
            author_user_id=user.id,
            author_label=user.display_name or user.email,
            mentioned_user_ids=[item["id"] for item in mention_user_items],
            task_id=task.id,
            task_log_id=log.id,
            task_title=task.title,
            comment=req.content,
            workspace_id=task.workspace_id,
        ))

    return TaskLogResponse(
        id=log.id, task_id=log.task_id, log_type=log.log_type,
        content=log.content, created_by=log.created_by,
        created_at=log.created_at.isoformat() if log.created_at else None,
        attachments=(log.meta or {}).get("attachments", []),
        author_agent_id=(log.meta or {}).get("agent_id"),
        author_agent_name=(log.meta or {}).get("agent_name"),
        meta=log.meta,
    )


@router.post("/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file attachment for a task. Returns metadata to include in a log."""
    import os
    from pathlib import Path

    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_writable(db, user, task.workspace_id)

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10 MB)")

    from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content
    try:
        trusted_content_type = await inspect_upload_content(
            data,
            filename=file.filename,
            declared_content_type=file.content_type,
        )
    except UploadSecurityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    filename = file.filename or "attachment"
    safe_name = filename.replace("/", "_").replace("\\", "_")

    from packages.core.services.entity_fs import (
        EntityFilesystemError,
        get_entity_root,
        write_entity_file_atomic,
    )

    # Deduplicate filename
    upload_dir = Path(get_entity_root(user.entity_id)) / "tasks" / task_id
    dest = upload_dir / safe_name
    base, ext = os.path.splitext(safe_name)
    counter = 1
    while dest.exists():
        dest = upload_dir / f"{base}_{counter}{ext}"
        counter += 1

    rel_path = f"tasks/{task_id}/{dest.name}"
    try:
        write_entity_file_atomic(
            user.entity_id,
            rel_path,
            data,
            expected_size=len(data),
            allow_empty=True,
        )
    except EntityFilesystemError as exc:
        raise HTTPException(
            503,
            f"Entity filesystem is not available: {exc}",
        ) from exc

    final_name = dest.name
    url = f"/api/v1/tasks/{task_id}/attachments/{final_name}"

    return {
        "filename": final_name,
        "original_name": filename,
        "size": len(data),
        "content_type": trusted_content_type,
        "url": url,
    }


@router.get("/{task_id}/attachments/{filename}")
async def download_task_attachment(
    task_id: str,
    filename: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a task attachment."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from packages.core.services.entity_fs import is_fs_enabled, get_entity_root

    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_workspace_readable(db, user, task.workspace_id)

    if not is_fs_enabled():
        raise HTTPException(503, "Filesystem not configured")

    filepath = Path(get_entity_root(user.entity_id)) / "tasks" / task_id / filename
    if not filepath.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(filepath), filename=filename)
