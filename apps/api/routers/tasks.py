"""Task endpoints — CRUD, status changes, processing logs, automation."""
from __future__ import annotations

import logging
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.constants.task import TASK_STATUSES, TASK_PRIORITIES, TASK_CATEGORIES, TASK_TYPES
from packages.core.constants.task_notifications import (
    task_notification_channels,
    task_notification_events,
)
from packages.core.services.task_service import (
    list_tasks, get_task, create_task, update_task,
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
from packages.core.services.task_state_machine import TaskStatusTransitionError
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_APPROVAL_ACCEPT_CHOICES = {"approve", "approved", "yes", "accept"}
_APPROVAL_REVISION_CHOICES = {
    "reject",
    "rejected",
    "no",
    "decline",
    "changes",
    "request_changes",
}
_APPROVAL_CHOICES = _APPROVAL_ACCEPT_CHOICES | _APPROVAL_REVISION_CHOICES


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


def _is_approval_task(task) -> bool:
    if getattr(task, "task_type", None) == "approval":
        return True
    details = task.details if isinstance(task.details, dict) else {}
    runtime_context = details.get("runtime_context") if isinstance(details, dict) else {}
    instructions = (
        str(runtime_context.get("instructions") or "")
        if isinstance(runtime_context, dict) else ""
    )
    text = " ".join([
        str(task.title or ""),
        str(task.description or ""),
        str(details.get("approval_decision") or ""),
        instructions,
    ]).lower()
    return "approval" in text or "approve" in text or "pending_founder_review" in text


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
    # Resolved display fields (populated by backend, not stored in DB)
    assignee_name: str | None = None
    assignee_avatar: str | None = None
    agent_name: str | None = None
    agent_avatar: str | None = None
    creator_name: str | None = None
    creator_avatar: str | None = None


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
    details: dict = {}
    deadline: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    assignee_id: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    category_id: str | None = None
    sla_policy_id: str | None = None
    details: dict | None = None
    deadline: str | None = None
    scheduled_at: str | None = None
    duration_minutes: int | None = None


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int


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
    log_type: str = "comment"
    attachments: list[dict] = []
    mentions: list[dict] = Field(default_factory=list, max_length=50)


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
    plan_id: str | None = None
    reset_steps: int = 0


class HITLResponseRequest(BaseModel):
    response: str | None = None
    choice: str | None = None
    fields: dict = {}
    note: str | None = None


class HITLResponseResponse(BaseModel):
    task: TaskResponse
    resumed: bool
    dispatched: bool
    mode: str | None = None
    plan_id: str | None = None
    step_id: str | None = None


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


async def _resolve_lookups(db: AsyncSession, tasks) -> tuple[dict, dict, dict, dict]:
    """Batch-load scoped user, staff, agent, and workspace display info for tasks."""
    from packages.core.models.staff import Staff
    from packages.core.models.user import User
    from packages.core.models.workspace import Agent, Workspace

    from packages.core.constants.agents import MANOR_AGENT_IDS

    entity_ids = {t.entity_id for t in tasks if getattr(t, "entity_id", None)}
    workspace_ids = {t.workspace_id for t in tasks if getattr(t, "workspace_id", None)}
    assignee_ids = {t.assignee_id for t in tasks if t.assignee_id}
    creator_ids = {t.creator_id for t in tasks if t.creator_id}
    actor_ids = assignee_ids | creator_ids
    user_ids = set(actor_ids)
    # Filter out manor-master — it's not a real DB agent.
    explicit_agent_ids = {t.agent_id for t in tasks if t.agent_id and t.agent_id not in MANOR_AGENT_IDS}

    users: dict = {}
    agents: dict = {}
    staff: dict = {}
    workspaces: dict = {}

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
                select(User).where(
                    User.id.in_(user_ids),
                    User.entity_id.in_(entity_ids),
                    User.deleted_at.is_(None),
                )
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

        # Normal agent assignment uses Task.agent_id. Be permissive for older
        # rows that stored an agent id in assignee_id, while still scoping
        # display resolution to this entity or public template agents.
        possible_agent_ids = (explicit_agent_ids | actor_ids) - MANOR_AGENT_IDS
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
                agents[a.id] = {"name": a.name, "avatar_url": getattr(a, "avatar_url", None)}
    except Exception as e:
        logger.warning("Failed to resolve task lookups: %s", e)

    return users, agents, staff, workspaces


def _to_response(
    t,
    users: dict | None = None,
    agents: dict | None = None,
    staff: dict | None = None,
    workspaces: dict | None = None,
) -> TaskResponse:
    """Convert a Task ORM object to TaskResponse with resolved display names.

    Args:
        users: optional {user_id: {name, avatar_url}} lookup
        agents: optional {agent_id: {name, avatar_url}} lookup
    """
    from packages.core.constants.agents import MANOR_AGENT_NAME, is_master_agent

    assignee_name = None
    assignee_avatar = None
    agent_name = None
    agent_avatar = None
    creator_name = None
    creator_avatar = None
    workspace_name = None

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

    if t.creator_id and is_master_agent(t.creator_id):
        creator_name = MANOR_AGENT_NAME
    if t.creator_id and not creator_name and users:
        cu = users.get(t.creator_id)
        if cu:
            creator_name = cu.get("name")
            creator_avatar = cu.get("avatar_url")
    if t.creator_id and not creator_name and staff:
        cs = staff.get(t.creator_id)
        if cs:
            creator_name = cs.get("name")
            creator_avatar = cs.get("avatar_url")
    if t.creator_id and not creator_name and agents:
        ca = agents.get(t.creator_id)
        if ca:
            creator_name = ca.get("name")
            creator_avatar = ca.get("avatar_url")

    if is_master_agent(t.agent_id, t.agent_type):
        agent_name = MANOR_AGENT_NAME
    elif t.agent_id and agents:
        a = agents.get(t.agent_id)
        if a:
            agent_name = a.get("name")
            agent_avatar = a.get("avatar_url")

    return TaskResponse(
        id=t.id, entity_id=t.entity_id, title=t.title,
        description=t.description, status=t.status, priority=t.priority,
        task_type=t.task_type, workspace_id=t.workspace_id,
        workspace_name=workspace_name,
        category_id=t.category_id, assignee_id=t.assignee_id,
        agent_id=t.agent_id, agent_type=t.agent_type,
        owner_service_key=getattr(t, "owner_service_key", None),
        owner_subscription_id=getattr(t, "owner_subscription_id", None),
        creator_id=t.creator_id,
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
        actual_output=getattr(t, "actual_output", None),
        deadline=t.deadline.isoformat() if t.deadline else None,
        scheduled_at=(t.details or {}).get("scheduled_at"),
        duration_minutes=(t.details or {}).get("duration_minutes"),
        started_at=t.started_at.isoformat() if t.started_at else None,
        completed_at=t.completed_at.isoformat() if t.completed_at else None,
        created_at=t.created_at.isoformat() if t.created_at else None,
        assignee_name=assignee_name, assignee_avatar=assignee_avatar,
        agent_name=agent_name, agent_avatar=agent_avatar,
        creator_name=creator_name, creator_avatar=creator_avatar,
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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    effective_workspace_id = workspace_id or workspace_id_alias
    tasks, total = await list_tasks(
        db, user.entity_id,
        status=status, workspace_id=effective_workspace_id,
        category_id=category_id,
        parent_task_id=parent_task_id,
        limit=limit, offset=offset,
    )
    users, agents, staff, workspaces = await _resolve_lookups(db, tasks)
    return TaskListResponse(items=[_to_response(t, users, agents, staff, workspaces) for t in tasks], total=total)


@router.post("", response_model=TaskResponse, status_code=201)
async def create_new_task(
    req: TaskCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await create_task(
        db, user.entity_id,
        title=req.title, description=req.description,
        priority=req.priority, task_type=req.task_type,
        workspace_id=req.workspace_id, category_id=req.category_id,
        assignee_id=req.assignee_id, agent_id=req.agent_id,
        agent_type=req.agent_type, creator_id=user.id,
        details=req.details, deadline=req.deadline,
        scheduled_at=req.scheduled_at, duration_minutes=req.duration_minutes,
    )

    # If assigned to an AI agent (or manor master), dispatch Celery task
    from packages.core.constants.agents import is_master_agent, MANOR_AGENT_ID
    if req.agent_id or is_master_agent(req.agent_id, req.agent_type):
        try:
            from packages.core.tasks.ai_tasks import run_agent_task
            dispatch_id = req.agent_id or MANOR_AGENT_ID
            run_agent_task.delay(task.id, dispatch_id)
            logger.info("Dispatched agent task: task=%s agent=%s type=%s", task.id, dispatch_id, req.agent_type)
        except Exception as e:
            logger.warning("Failed to dispatch agent task: %s", e)

    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return _to_response(task, users, agents, staff, workspaces)


@router.post("/from-template", response_model=TaskResponse, status_code=201)
async def create_from_template(
    req: TaskFromTemplateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new task pre-filled from a task template."""
    overrides = req.model_dump(exclude={"template_id"}, exclude_none=True)
    from packages.core.services.template_service import instantiate_template
    try:
        task = await instantiate_template(
            db, user.entity_id, req.template_id,
            variables=overrides, creator_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return _to_response(task, users, agents, staff, workspaces)


@router.get("/board", response_model=dict)
async def task_board(
    workspace_id: str | None = Query(None),
    workspace_id_alias: str | None = Query(None, alias="workspaceId"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tasks grouped by status for Kanban board view."""
    effective_workspace_id = workspace_id or workspace_id_alias
    board = await get_tasks_by_status(db, user.entity_id, workspace_id=effective_workspace_id)
    counts = board.pop("_counts", {})
    all_tasks = [t for tasks in board.values() for t in tasks]
    users, agents, staff, workspaces = await _resolve_lookups(db, all_tasks)
    result = {status: [_to_response(t, users, agents, staff, workspaces) for t in tasks] for status, tasks in board.items()}
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
    try:
        task = await move_task(db, task_id, user.entity_id, req.status)
    except TaskStatusTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "Task not found")
    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return _to_response(task, users, agents, staff, workspaces)


@router.post("/{task_id}/retry", response_model=RetryTaskResponse)
async def retry_task_endpoint(
    task_id: str,
    req: RetryTaskRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually retry a failed/blocked/HITL task.

    For plan-backed tasks, reset failed or waiting steps and re-enqueue
    the plan runner. For legacy agent tasks, re-dispatch TaskRunner.
    """
    from datetime import datetime, timezone

    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.status in ("completed", "cancelled"):
        raise HTTPException(409, f"Task is {task.status} and cannot be retried")
    if task.status == "in_progress":
        raise HTTPException(409, "Task is already in progress")

    note = (req.note if req else None) or None
    now = datetime.now(timezone.utc)
    details = dict(task.details or {})
    retry_meta = {
        "requested_by": user.id,
        "requested_at": now.isoformat(),
    }
    if note:
        retry_meta["note"] = note
    retry_count = int(details.get("manual_retry_count") or 0) + 1
    details["manual_retry"] = retry_meta
    details["manual_retry_count"] = retry_count

    from packages.core.services.task_dependencies import dependency_ids_from_details, details_with_dependency_state
    dep_ids = dependency_ids_from_details(details)
    if dep_ids:
        details = await details_with_dependency_state(db, task, details)
        if details.get("dependency_status") != "completed":
            raise HTTPException(
                409,
                "Task dependencies are not completed yet; waiting for predecessor outputs.",
            )

    mode = ""
    plan_id: str | None = None
    reset_steps = 0
    reset_step_ids: list[str] = []
    dispatch = None

    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    plan = (await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.task_id == task.id,
            ExecutionPlan.entity_id == user.entity_id,
        ).order_by(ExecutionPlan.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if plan and plan.status not in ("completed", "cancelled"):
        mode = "plan"
        plan_id = plan.id
        steps = list((await db.execute(
            select(ExecutionStep).where(ExecutionStep.plan_id == plan.id)
        )).scalars().all())
        retryable_statuses = {"failed", "skipped", "waiting_human", "paused", "cancelled"}
        for step in steps:
            if step.step_status in retryable_statuses:
                step.step_status = "pending"
                step.current_lease_id = None
                step.human_input_prompt = None
                step.human_input_response = (
                    {"response": note, "user": user.display_name or user.email}
                    if note else None
                )
                step.error = None
                step.finished_at = None
                step.attempt_count = 0
                reset_steps += 1
                reset_step_ids.append(step.id)
        if plan.status in ("failed", "needs_attention", "paused"):
            plan.status = "draft"
            plan.completed_at = None
            plan.last_error = None
        dispatch = ("plan", plan.id)
    elif task.owner_subscription_id or task.owner_service_key:
        mode = "plan_new"
        dispatch = ("plan_new", task.id)
    else:
        from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
        if task.agent_id or is_master_agent(task.agent_id, task.agent_type):
            mode = "agent"
            if note:
                details["_hitl_response"] = note
                details["_hitl_responded_by"] = user.display_name or user.email
            dispatch = ("agent", task.agent_id or MANOR_AGENT_ID)
        else:
            raise HTTPException(409, "Task has no plan, owner subscription, or assigned agent to retry")

    from packages.core.services.task_state_machine import apply_task_status_transition
    apply_task_status_transition(task, "in_progress", now=now)
    task.started_at = now
    task.completed_at = None
    task.details = details
    task.actual_output = None
    await add_task_log(
        db, task.id, "manual_retry",
        "Manual retry requested" + (f": {note}" if note else ""),
        created_by=user.display_name or user.email,
        metadata={
            "mode": mode,
            "plan_id": plan_id,
            "step_ids": reset_step_ids,
            "reset_steps": reset_steps,
            "retry_count": retry_count,
            "requested_by": user.id,
        },
    )
    from packages.core.services import event_emitter
    event_emitter.emit(
        user.entity_id,
        "task.retried",
        source="tasks_api",
        payload={
            "task_id": task.id,
            "plan_id": plan_id,
            "step_ids": reset_step_ids,
            "mode": mode,
            "reset_steps": reset_steps,
            "retry_count": retry_count,
            "requested_by": user.id,
        },
    )
    await db.commit()
    await db.refresh(task)

    dispatched = False
    try:
        kind, value = dispatch
        if kind == "plan":
            from packages.core.tasks.ai_tasks import run_plan
            run_plan.delay(value)
        elif kind == "plan_new":
            from packages.core.tasks.ai_tasks import plan_and_run_task
            plan_and_run_task.delay(value)
        elif kind == "agent":
            from packages.core.tasks.ai_tasks import run_agent_task
            run_agent_task.delay(task.id, value)
        dispatched = True
    except Exception as exc:
        logger.warning("Task retry dispatch failed: task=%s mode=%s error=%s", task.id, mode, exc)

    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return RetryTaskResponse(
        task=_to_response(task, users, agents, staff, workspaces),
        dispatched=dispatched,
        mode=mode,
        plan_id=plan_id,
        reset_steps=reset_steps,
    )


async def _resume_hitl(
    db: AsyncSession,
    task,
    user: User,
    *,
    response_text: str,
    payload: dict,
) -> dict:
    """Resume a task waiting on structured human input."""
    from datetime import datetime, timezone

    submitted_by = user.display_name or user.email
    now = datetime.now(timezone.utc)
    meta = {
        "response": response_text,
        "choice": payload.get("choice"),
        "fields": payload.get("fields") or {},
        "submitted_by": user.id,
        "submitted_at": now.isoformat(),
    }

    # Plan-based HITL: resume the newest waiting_human step.
    try:
        from packages.core.models.execution import ExecutionPlan, ExecutionStep
        from packages.core.services.task_state_machine import apply_task_status_transition

        plan = (await db.execute(
            select(ExecutionPlan).where(
                ExecutionPlan.task_id == task.id,
                ExecutionPlan.entity_id == user.entity_id,
                ExecutionPlan.status.in_(("running", "paused", "needs_attention")),
            ).order_by(ExecutionPlan.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if plan:
            waiting_step = (await db.execute(
                select(ExecutionStep).where(
                    ExecutionStep.plan_id == plan.id,
                    ExecutionStep.step_status == "waiting_human",
                ).order_by(ExecutionStep.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if waiting_step:
                waiting_step.human_input_response = {
                    **meta,
                    "user": submitted_by,
                    "payload": payload,
                }
                waiting_step.step_status = "pending"
                waiting_step.human_input_prompt = None
                waiting_step.current_lease_id = None
                plan.status = "running"
                plan.completed_at = None
                plan.last_error = None
                if task.status == "waiting_on_customer":
                    apply_task_status_transition(task, "in_progress", now=now)
                if waiting_step.kind == "human":
                    try:
                        from packages.core.temporal_app import signal_human_input
                        await signal_human_input(
                            plan.id,
                            waiting_step.step_key,
                            waiting_step.human_input_response,
                        )
                    except Exception:
                        pass
                await add_task_log(
                    db,
                    task.id,
                    "ai_hitl_resumed",
                    f"Human input received: {response_text[:300]}" if response_text else "Human input received.",
                    created_by=submitted_by,
                    metadata={
                        **meta,
                        "mode": "plan",
                        "plan_id": plan.id,
                        "step_id": waiting_step.id,
                    },
                )
                await db.commit()
                dispatched = False
                try:
                    from packages.core.tasks.ai_tasks import run_plan
                    run_plan.delay(plan.id)
                    dispatched = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Plan HITL resume dispatch failed: plan=%s error=%s", plan.id, exc)
                return {
                    "resumed": True,
                    "dispatched": dispatched,
                    "mode": "plan",
                    "plan_id": plan.id,
                    "step_id": waiting_step.id,
                }
    except Exception as exc:
        logger.warning("Plan HITL structured response failed: %s", exc)

    # Legacy TaskRunner HITL: stash the structured response and re-dispatch.
    from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
    has_agent = task.agent_id or is_master_agent(task.agent_id, task.agent_type)
    if task.status == "waiting_on_customer" and has_agent:
        details = dict(task.details or {})
        details["_hitl_response"] = response_text
        details["_hitl_payload"] = payload
        details["_hitl_responded_by"] = submitted_by
        await update_task(db, task.id, user.entity_id, status="pending", details=details)
        await add_task_log(
            db,
            task.id,
            "ai_hitl_resumed",
            f"Human input received: {response_text[:300]}" if response_text else "Human input received.",
            created_by=submitted_by,
            metadata={**meta, "mode": "agent"},
        )
        dispatch_id = task.agent_id or MANOR_AGENT_ID
        await db.commit()
        dispatched = False
        try:
            from packages.core.tasks.ai_tasks import run_agent_task
            run_agent_task.delay(task.id, dispatch_id)
            dispatched = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent HITL resume dispatch failed: task=%s error=%s", task.id, exc)
        return {
            "resumed": True,
            "dispatched": dispatched,
            "mode": "agent",
            "plan_id": None,
            "step_id": None,
        }

    return {
        "resumed": False,
        "dispatched": False,
        "mode": None,
        "plan_id": None,
        "step_id": None,
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

    response_text = (req.response or req.note or "").strip()
    fields = req.fields or {}
    if not response_text and not req.choice and not fields:
        raise HTTPException(400, "response, choice, or fields is required")

    payload = {
        "response": response_text,
        "choice": req.choice,
        "fields": fields,
        "note": req.note,
    }
    result = await _resume_hitl(db, task, user, response_text=response_text, payload=payload)
    if not result["resumed"]:
        raise HTTPException(409, "Task is not waiting for structured human input")

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
    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return HITLResponseResponse(
        task=_to_response(task, users, agents, staff, workspaces),
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
    from datetime import datetime, timezone

    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not _is_approval_task(task):
        raise HTTPException(400, "Task is not an approval task")
    if task.status in {"completed", "cancelled", "failed"}:
        raise HTTPException(409, "Approval task is already closed")

    choice = (req.choice or "").strip().lower()
    if choice not in _APPROVAL_CHOICES:
        raise HTTPException(400, "choice must be approve, reject, or request_changes")
    approved = choice in _APPROVAL_ACCEPT_CHOICES
    decision = "approved" if approved else "changes_requested"
    note = (req.note or "").strip()
    actor = user.display_name or user.email
    decided_at = datetime.now(timezone.utc).isoformat()
    details = dict(task.details or {})
    details["approval_decision"] = {
        "decision": decision,
        "choice": choice,
        "approved": approved,
        "note": note,
        "decided_by": user.id,
        "decided_by_label": actor,
        "decided_at": decided_at,
    }
    actual_output = {
        "summary": (
            f"Approval task {decision.replace('_', ' ')}"
            + (f": {note}" if note else "")
        ),
        "approval": details["approval_decision"],
    }

    updated = await update_task(
        db,
        task.id,
        user.entity_id,
        status="completed",
        details=details,
        actual_output=actual_output,
    )
    log = await add_task_log(
        db,
        task.id,
        "approval_decision",
        (
            f"{actor} approved this task."
            if approved else
            f"{actor} requested changes for this approval task."
        ) + (f"\n\n{note}" if note else ""),
        created_by=actor,
        metadata=details["approval_decision"],
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

    users, agents, staff, workspaces = await _resolve_lookups(db, [updated])
    return _to_response(updated, users, agents, staff, workspaces)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_one_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        from packages.core.services.task_execution_reconcile import reconcile_task_from_latest_completed_plan

        await reconcile_task_from_latest_completed_plan(db, task)
    except Exception:
        logger.debug("Task execution reconciliation skipped for %s", task_id, exc_info=True)
    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return _to_response(task, users, agents, staff, workspaces)


@router.delete("/{task_id}", status_code=204)
async def delete_one_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task(db, task_id, user.entity_id)
    if not task:
        raise HTTPException(404, "Task not found")
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
    old_agent_id = old_task.agent_id if old_task else None
    old_agent_type = old_task.agent_type if old_task else None
    old_status = old_task.status if old_task else None

    # Build update payload — empty strings mean "clear this field"
    update_data = {}
    for key, val in req.model_dump(exclude_none=True).items():
        if key in ("scheduled_at", "duration_minutes"):
            continue  # handled below via details merge
        if isinstance(val, str) and val == "" and key in ("assignee_id", "agent_id", "agent_type", "category_id"):
            update_data[key] = None
        else:
            update_data[key] = val
    if "details" in update_data:
        update_data["details"] = _merge_task_details(
            old_task.details if old_task else None,
            update_data.get("details"),
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
    try:
        task = await update_task(db, task_id, user.entity_id, user_id=user.id, **update_data)
    except TaskStatusTransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not task:
        raise HTTPException(404, "Task not found")

    # If agent assignment changed, dispatch Celery task
    from packages.core.constants.agents import is_master_agent as _is_master, MANOR_AGENT_ID as _MANOR_ID
    new_agent_id = update_data.get("agent_id")
    new_agent_type = update_data.get("agent_type")
    was_master = _is_master(old_agent_id, old_agent_type)
    now_master = _is_master(new_agent_id, new_agent_type)
    is_new_agent = (new_agent_id and new_agent_id != old_agent_id) or (now_master and not was_master)
    if is_new_agent and task.status not in ("completed", "cancelled", "failed"):
        try:
            from packages.core.tasks.ai_tasks import run_agent_task
            dispatch_id = new_agent_id or _MANOR_ID
            run_agent_task.delay(task.id, dispatch_id)
            logger.info("Dispatched agent task (reassigned): task=%s agent=%s type=%s", task.id, dispatch_id, new_agent_type)
        except Exception as e:
            logger.warning("Failed to dispatch agent task: %s", e)

    # HITL resumption: status changed from waiting back to actionable while agent is assigned
    new_status = update_data.get("status")
    if (old_status == "waiting_on_customer" and new_status in ("pending", "in_progress")
        and (task.agent_id or _is_master(task.agent_id, task.agent_type))):
        try:
            from packages.core.tasks.ai_tasks import run_agent_task
            dispatch_id = task.agent_id or _MANOR_ID
            run_agent_task.delay(task.id, dispatch_id)
            logger.info("HITL resumed (status change): task=%s agent=%s", task.id, dispatch_id)
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
        await db.flush()

    users, agents, staff, workspaces = await _resolve_lookups(db, [task])
    return _to_response(task, users, agents, staff, workspaces)


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
    meta = {}
    if req.attachments:
        meta["attachments"] = req.attachments
    mention_agent_items: list[dict] = []
    mention_user_items: list[dict] = []
    if req.log_type == "comment" and req.mentions:
        mention_agent_items, mention_user_items = await validate_mentions(
            db, entity_id=user.entity_id, raw=req.mentions,
        )
        if mention_agent_items or mention_user_items:
            meta["mentions"] = mention_agent_items + mention_user_items
    log = await add_task_log(
        db, task_id, req.log_type, req.content,
        created_by=(user.display_name or user.email),
        metadata=meta if meta else None,
    )

    hitl_result = {"resumed": False}
    queued_learning_ids: list[str] = []
    # HITL resumption: comment on a waiting task resumes execution
    if task.status in ("waiting_on_customer", "in_progress") and req.log_type == "comment":
        payload = {"response": req.content, "fields": {}, "source": "comment"}
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
        req.log_type == "comment"
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
        req.log_type == "comment"
        and bool((req.content or "").strip())
        and bool(task.workspace_id)
        and not bool(hitl_result.get("resumed"))
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

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10 MB)")

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
        "content_type": file.content_type or "application/octet-stream",
        "url": url,
    }


@router.get("/{task_id}/attachments/{filename}")
async def download_task_attachment(
    task_id: str,
    filename: str,
    user: User = Depends(get_current_user),
):
    """Download a task attachment."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    from packages.core.services.entity_fs import is_fs_enabled, get_entity_root

    if not is_fs_enabled():
        raise HTTPException(503, "Filesystem not configured")

    filepath = Path(get_entity_root(user.entity_id)) / "tasks" / task_id / filename
    if not filepath.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(filepath), filename=filename)
