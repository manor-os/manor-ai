"""Workspace endpoints — CRUD, operating model, agent mappings, activity, setup."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, delete as sa_delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import DocumentGroup
from packages.core.services.entity_service import (
    WORKSPACE_PURGE_GRACE_DAYS,
    list_workspaces, get_workspace, create_workspace, update_workspace,
    soft_delete_workspace, restore_workspace, list_trashed_workspaces,
)
from packages.core.services.workspace_dashboard_service import (
    get_workspace_stats,
    get_workspace_custom_field_summary,
)
from packages.core.services.workspace_runtime import (
    sync_workspace_runtime_schedules,
)
from packages.core.services.workspace_access import (
    ensure_workspace_owner_membership,
    filter_workspaces_for_user,
    settings_with_default_workspace_access,
    user_can_read_workspace,
)
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
    provider_keys_match,
)
from packages.core.services.tool_cache_version import bump_tool_cache_version
from apps.api.deps import get_current_user, require_plan

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


# ── Request / Response models ────────────────────────────────────────────────

_SUPPORTED_CHANNEL_LANGUAGES = {"en", "zh", "es", "de"}


def _normalize_channel_language(value: Any) -> str:
    base = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return base if base in _SUPPORTED_CHANNEL_LANGUAGES else "en"


def _normalized_channel_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    raw_language = cfg.get("language") or cfg.pop("locale", None)
    cfg["language"] = _normalize_channel_language(raw_language)
    return cfg


class WorkspaceResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    description: str | None = None
    category: str | None = None
    address: str | None = None
    kind: str | None = None
    operating_context: str | None = None
    primary_work: str | None = None
    operating_model: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_user_id: str | None = None
    created_by_name: str | None = None
    created_by_email: str | None = None
    created_by_avatar_url: str | None = None
    # Extended fields
    longitude: float | None = None
    latitude: float | None = None
    cover_image_url: str | None = None
    attribute_tags: list[str] = Field(default_factory=list)
    identity_label: str | None = None
    property_type: str | None = None
    occupancy_status: str | None = None
    pms_property_id: str | None = None
    pms_unit_id: str | None = None
    heartbeat_enabled: bool = False
    heartbeat_cadence: str | None = None
    last_heartbeat_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    deleted_at: datetime | None = None


class WorkspaceCreateRequest(BaseModel):
    name: str
    description: str = ""
    category: str = ""
    address: str = ""
    kind: str = ""
    operating_context: str = ""
    primary_work: str = ""
    # Extended fields
    longitude: float | None = None
    latitude: float | None = None
    cover_image_url: str | None = None
    attribute_tags: list[str] = Field(default_factory=list)
    identity_label: str | None = None
    property_type: str | None = None
    occupancy_status: str | None = None
    pms_property_id: str | None = None
    pms_unit_id: str | None = None
    heartbeat_enabled: bool = False
    heartbeat_cadence: str | None = None


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    address: str | None = None
    kind: str | None = None
    operating_context: str | None = None
    primary_work: str | None = None
    # Extended fields
    longitude: float | None = None
    latitude: float | None = None
    cover_image_url: str | None = None
    attribute_tags: list[str] | None = None
    identity_label: str | None = None
    property_type: str | None = None
    occupancy_status: str | None = None
    pms_property_id: str | None = None
    pms_unit_id: str | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_cadence: str | None = None
    settings: dict | None = None


class StaffAssignRequest(BaseModel):
    staff_id: str
    role: str | None = None
    # Permission-v1: workspace-level role (owner / editor / contributor /
    # viewer / external_client) + optional expiry. Backward compat: legacy
    # callers that didn't send these get permanent assignment with no
    # role enum validation.
    expires_at: datetime | None = None
    user_id: str | None = None


class StaffResponse(BaseModel):
    id: str
    workspace_id: str
    staff_id: str | None = None
    user_id: str | None = None
    role: str | None = None
    added_by: str | None = None
    added_at: datetime | None = None
    expires_at: datetime | None = None
    status: str | None = None
    created_at: datetime | None = None


class WorkspaceKnowledgeGroupCreateRequest(BaseModel):
    name: str
    purpose: str | None = None
    kind: str | None = None


class WorkspaceKnowledgeGroupUpdateRequest(BaseModel):
    name: str | None = None
    purpose: str | None = None
    kind: str | None = None


class WorkspaceKnowledgeMembersRequest(BaseModel):
    document_ids: list[str] = Field(default_factory=list)


class ServiceRequest(BaseModel):
    key: str
    name: str
    description: str = ""
    config: dict = Field(default_factory=dict)


class AgentMappingRequest(BaseModel):
    service_key: str
    agent_id: str
    custom_prompt: str | None = None


class WorkspaceChannelRequest(BaseModel):
    channel_config_id: str | None = None
    channel_type: str = "webchat"
    name: str | None = None
    purpose: str | None = None
    role: str = "primary_external"
    linked_service_key: str | None = None
    agent_subscription_id: str | None = None
    agent_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceChannelUpdateRequest(BaseModel):
    name: str | None = None
    purpose: str | None = None
    role: str | None = None
    linked_service_key: str | None = None
    agent_subscription_id: str | None = None
    agent_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class GoalsRequest(BaseModel):
    goals: list[dict[str, Any]]


class RulesRequest(BaseModel):
    rules: list[dict[str, Any]]


class RuntimeEvidenceResponse(BaseModel):
    id: str
    workspace_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    evidence_type: str
    source: str
    status: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class LearningCandidateResponse(BaseModel):
    id: str
    workspace_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    candidate_type: str
    scope: str
    title: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    risk_level: str
    status: str
    confidence: float
    created_by: str
    resolution: dict[str, Any] = Field(default_factory=dict)
    applied_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LearningCandidateResolveRequest(BaseModel):
    status: str = Field(..., pattern="^(proposed|accepted|rejected|archived)$")
    note: str | None = None


class OperationDraftCreateRequest(BaseModel):
    source_event_id: str | None = None
    patches: list[dict[str, Any]] = Field(default_factory=list)


class OperationPatchRequest(BaseModel):
    patches: list[dict[str, Any]] = Field(default_factory=list)


class OperationApplyRequest(BaseModel):
    user_confirmation: bool = True


class SetupTurnRequest(BaseModel):
    session_id: str | None = None
    message: str


class SetupFinalizeRequest(BaseModel):
    session_id: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _user_display_name(user: User | None) -> str | None:
    if not user:
        return None
    full_name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        if part
    ).strip()
    return getattr(user, "display_name", None) or full_name or getattr(user, "email", None)


def _user_summary(user: User | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user.id,
        "name": _user_display_name(user),
        "email": user.email,
        "avatar_url": getattr(user, "avatar_url", None),
    }


def _coerce_user_summary(user: User | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(user, dict):
        return user
    return _user_summary(user)


async def _workspace_creator_summaries(
    db: AsyncSession,
    workspaces: list[Any],
) -> dict[str, dict[str, Any]]:
    workspace_ids = [ws.id for ws in workspaces if getattr(ws, "id", None)]
    if not workspace_ids:
        return {}

    configured_ids: dict[str, str] = {}
    for ws in workspaces:
        settings = ws.settings if isinstance(getattr(ws, "settings", None), dict) else {}
        user_id = settings.get("created_by_user_id")
        if isinstance(user_id, str) and user_id:
            configured_ids[ws.id] = user_id

    staff_rows = list((await db.execute(
        select(WorkspaceStaff).where(WorkspaceStaff.workspace_id.in_(workspace_ids))
    )).scalars().all())
    staff_rows.sort(
        key=lambda row: (
            row.workspace_id,
            (row.added_at or row.created_at).isoformat()
            if (row.added_at or row.created_at)
            else "9999",
        )
    )

    candidate_ids = set(configured_ids.values())
    for row in staff_rows:
        if row.user_id:
            candidate_ids.add(row.user_id)
        if row.added_by:
            candidate_ids.add(row.added_by)
    if not candidate_ids:
        return {}

    users = list((await db.execute(
        select(User).where(User.id.in_(list(candidate_ids)), User.deleted_at.is_(None))
    )).scalars().all())
    users_by_id = {user.id: user for user in users}

    result: dict[str, dict[str, Any]] = {}
    for workspace_id, user_id in configured_ids.items():
        if user := users_by_id.get(user_id):
            result[workspace_id] = _user_summary(user) or {}

    for row in staff_rows:
        if row.workspace_id in result or row.role != "owner":
            continue
        candidate_id = row.user_id or row.added_by
        if candidate_id and (user := users_by_id.get(candidate_id)):
            result[row.workspace_id] = _user_summary(user) or {}

    for row in staff_rows:
        if row.workspace_id in result:
            continue
        candidate_id = row.user_id or row.added_by
        if candidate_id and (user := users_by_id.get(candidate_id)):
            result[row.workspace_id] = _user_summary(user) or {}

    return result


def _to_response(ws, *, creator: User | dict[str, Any] | None = None) -> WorkspaceResponse:
    creator_summary = _coerce_user_summary(creator)
    return WorkspaceResponse(
        id=ws.id, entity_id=ws.entity_id, name=ws.name,
        description=ws.description, category=ws.category,
        address=ws.address, kind=ws.kind,
        operating_context=ws.operating_context,
        primary_work=ws.primary_work,
        operating_model=ws.operating_model or {},
        settings=ws.settings or {}, status=ws.status,
        created_at=ws.created_at, updated_at=getattr(ws, "updated_at", None),
        created_by_user_id=(creator_summary or {}).get("id"),
        created_by_name=(creator_summary or {}).get("name"),
        created_by_email=(creator_summary or {}).get("email"),
        created_by_avatar_url=(creator_summary or {}).get("avatar_url"),
        longitude=float(ws.longitude) if ws.longitude is not None else None,
        latitude=float(ws.latitude) if ws.latitude is not None else None,
        cover_image_url=ws.cover_image_url,
        attribute_tags=ws.attribute_tags or [],
        identity_label=ws.identity_label,
        property_type=ws.property_type,
        occupancy_status=ws.occupancy_status,
        pms_property_id=ws.pms_property_id,
        pms_unit_id=ws.pms_unit_id,
        heartbeat_enabled=ws.heartbeat_enabled or False,
        heartbeat_cadence=ws.heartbeat_cadence,
        last_heartbeat_at=ws.last_heartbeat_at,
        deleted_at=getattr(ws, "deleted_at", None),
    )


async def _require_workspace(db: AsyncSession, workspace_id: str, entity_id: str):
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


async def _require_workspace_read(db: AsyncSession, workspace_id: str, user: User):
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    if not await user_can_read_workspace(db, workspace=ws, user=user):
        raise HTTPException(404, "Workspace not found")
    return ws


async def _workspace_role_of(
    db: AsyncSession, workspace_id: str, user_id: str | None
) -> str | None:
    """Return the caller's active membership role in this workspace, or None."""
    if not user_id:
        return None
    row = (
        await db.execute(
            select(WorkspaceStaff)
            .where(
                WorkspaceStaff.workspace_id == workspace_id,
                WorkspaceStaff.user_id == user_id,
                WorkspaceStaff.status == "active",
            )
            .limit(1)
        )
    ).scalars().first()
    return row.role if row else None


async def _require_workspace_manage(db: AsyncSession, workspace_id: str, user: User):
    """Authorize a workspace-management action (settings update, staff
    add/remove).

    Allowed for an entity owner/admin (firm-wide) OR a member holding the
    workspace ``owner`` role. Previously these mutating endpoints gated on
    entity scope only, so ANY entity member — including a viewer — could
    rename a workspace or assign themselves as its owner. This closes that
    hole. Workspace creators are auto-enrolled as ``owner`` at create time
    so a non-admin creator is never locked out of their own workspace.
    """
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    if user.role in ("owner", "admin"):
        return ws
    if await _workspace_role_of(db, workspace_id, user.id) == "owner":
        return ws
    if not await user_can_read_workspace(db, workspace=ws, user=user):
        raise HTTPException(404, "Workspace not found")
    raise HTTPException(
        403,
        "Only an entity owner/admin or the workspace owner can manage this workspace",
    )


async def _apply_workspace_operation_patches(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    patches: list[dict[str, Any]],
    source_event_id: str,
):
    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        OperationValidationError,
        apply_operation_draft,
        create_operation_draft,
    )

    draft = await create_operation_draft(
        db,
        workspace_id,
        entity_id,
        user_id=user_id,
        source_event_id=source_event_id,
        initial_patches=patches,
    )
    if draft is None:
        raise HTTPException(404, "Workspace not found")
    try:
        return await apply_operation_draft(
            db,
            draft.id,
            entity_id,
            workspace_id,
            user_id=user_id,
            user_confirmation=True,
        )
    except OperationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OperationValidationError as exc:
        raise HTTPException(400, exc.validation) from exc


def _invalidate_workspace_context(workspace_id: str) -> None:
    """Drop cached workspace chat summary after runtime-visible changes."""
    from packages.core.workspace_chat.context import invalidate

    invalidate(workspace_id)


async def _mark_workspace_knowledge_changed(entity_id: str, workspace_id: str) -> None:
    await bump_tool_cache_version(entity_id, "documents")
    _invalidate_workspace_context(workspace_id)


async def _mark_workspace_staff_changed(entity_id: str, workspace_id: str) -> None:
    await bump_tool_cache_version(entity_id, "staff")
    _invalidate_workspace_context(workspace_id)


async def _ensure_user_staff_id(db: AsyncSession, user: User) -> str:
    """Return the Staff row for a user, creating one for legacy tenants.

    Some deployed databases still enforce ``workspace_staff.staff_id`` as
    NOT NULL, while newer code also stores direct ``user_id`` memberships.
    Keep both populated so workspace creation works across both schemas.
    """
    from packages.core.models.base import generate_ulid
    from packages.core.models.staff import Staff
    from packages.core.services.auth_service import ensure_user_membership

    staff = (await db.execute(
        select(Staff).where(
            Staff.entity_id == user.entity_id,
            Staff.user_id == user.id,
            Staff.deleted_at.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if staff is None:
        staff = Staff(
            id=generate_ulid(),
            entity_id=user.entity_id,
            kind="employee",
            name=user.display_name or user.email.split("@")[0],
            email=user.email,
            avatar_url=user.avatar_url,
            user_id=user.id,
            meta={"role": user.role},
            status="active",
        )
        db.add(staff)
        await db.flush()
    elif not (staff.meta or {}).get("role"):
        meta = dict(staff.meta or {})
        meta["role"] = user.role
        staff.meta = meta
        await db.flush()

    await ensure_user_membership(
        db,
        user=user,
        entity_id=user.entity_id,
        role=user.role,
        status="active",
        staff_id=staff.id,
        is_primary=True,
    )
    return staff.id


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[WorkspaceResponse])
async def list_my_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func
    from packages.core.models.task import Task
    from packages.core.models.goal import Goal
    from packages.core.models.workspace import AgentSubscription

    workspaces = await list_workspaces(db, user.entity_id)
    workspaces = await filter_workspaces_for_user(db, workspaces=workspaces, user=user)

    # Batch-load lightweight stats for all workspaces
    ws_ids = [ws.id for ws in workspaces]
    stats_map: dict[str, dict] = {
        wid: {
            "tasks": 0,
            "tasks_active": 0,
            "goals": 0,
            "agents": 0,
            "pending_actions": 0,
            "chat_pending_actions": 0,
            "proposal_actions": 0,
            "failed_actions": 0,
            "hitl_tasks": 0,
        }
        for wid in ws_ids
    }

    if ws_ids:
        # Task counts
        task_rows = (await db.execute(
            select(Task.workspace_id, Task.status, func.count().label("cnt"))
            .where(
                Task.workspace_id.in_(ws_ids),
                Task.entity_id == user.entity_id,
            )
            .group_by(Task.workspace_id, Task.status)
        )).all()
        for r in task_rows:
            stats_map[r.workspace_id]["tasks"] += r.cnt
            if r.status in ("pending", "in_progress"):
                stats_map[r.workspace_id]["tasks_active"] += r.cnt

        # Goal counts
        goal_rows = (await db.execute(
            select(Goal.workspace_id, func.count().label("cnt"))
            .where(
                Goal.workspace_id.in_(ws_ids),
                Goal.entity_id == user.entity_id,
                Goal.status == "active",
            )
            .group_by(Goal.workspace_id)
        )).all()
        for r in goal_rows:
            stats_map[r.workspace_id]["goals"] = r.cnt

        # Agent counts
        agent_rows = (await db.execute(
            select(AgentSubscription.workspace_id, func.count().label("cnt"))
            .where(
                AgentSubscription.workspace_id.in_(ws_ids),
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.status == "active",
            )
            .group_by(AgentSubscription.workspace_id)
        )).all()
        for r in agent_rows:
            stats_map[r.workspace_id]["agents"] = r.cnt

        # Sidebar pending_actions covers unresolved chat actions that are
        # visible in workspace chat. Proposal approvals and failed-review
        # retries are tracked separately so the UI can style those badges
        # differently.
        from packages.core.models.task import Message, Conversation
        from packages.core.models.execution import ExecutionPlan, ExecutionStep
        chat_hitl_step_ids: dict[str, set[str]] = {}
        chat_hitl_task_ids: dict[str, set[str]] = {}
        try:
            pending_rows = (await db.execute(
                select(Conversation.workspace_id, Message.pending_action)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.workspace_id.in_(ws_ids),
                    Conversation.entity_id == user.entity_id,
                    Message.pending_action.isnot(None),
                    Message.pending_action["kind"].as_string().isnot(None),
                    Message.resolved_at.is_(None),
                )
            )).all()
            for r in pending_rows:
                action = r.pending_action or {}
                action_kind = action.get("kind")
                if action_kind in {
                    "human_input",
                    "governance_approval",
                    "workspace_operation_review",
                    "needs_input",
                    "needs_confirmation",
                }:
                    stats_map[r.workspace_id]["chat_pending_actions"] += 1
                    if action.get("step_id"):
                        chat_hitl_step_ids.setdefault(r.workspace_id, set()).add(action["step_id"])
                    if action.get("task_id"):
                        chat_hitl_task_ids.setdefault(r.workspace_id, set()).add(action["task_id"])
                elif action_kind == "approve_proposals":
                    stats_map[r.workspace_id]["proposal_actions"] += 1
                elif action_kind == "retry_strategist_review":
                    stats_map[r.workspace_id]["failed_actions"] += 1
        except Exception:
            pass  # Message model may not have these columns yet

        hitl_task_ids: dict[str, set[str]] = {}
        waiting_step_ids: dict[str, set[str]] = {}
        waiting_step_task_ids: dict[str, set[str]] = {}

        waiting_task_rows = (await db.execute(
            select(Task.workspace_id, Task.id)
            .where(
                Task.workspace_id.in_(ws_ids),
                Task.entity_id == user.entity_id,
                Task.status == "waiting_on_customer",
            )
        )).all()
        for r in waiting_task_rows:
            hitl_task_ids.setdefault(r.workspace_id, set()).add(r.id)

        waiting_step_rows = (await db.execute(
            select(ExecutionPlan.workspace_id, ExecutionStep.id, ExecutionPlan.task_id)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionStep.plan_id)
            .where(
                ExecutionPlan.workspace_id.in_(ws_ids),
                ExecutionPlan.entity_id == user.entity_id,
                ExecutionStep.entity_id == user.entity_id,
                ExecutionStep.step_status == "waiting_human",
                ExecutionPlan.task_id.isnot(None),
            )
        )).all()
        for r in waiting_step_rows:
            waiting_step_ids.setdefault(r.workspace_id, set()).add(r.id)
            waiting_step_task_ids.setdefault(r.workspace_id, set()).add(r.task_id)
            hitl_task_ids.setdefault(r.workspace_id, set()).add(r.task_id)

        for wid in ws_ids:
            hitl_ids = hitl_task_ids.get(wid, set())
            chat_step_ids = chat_hitl_step_ids.get(wid, set())
            step_ids = waiting_step_ids.get(wid, set())
            step_task_ids = waiting_step_task_ids.get(wid, set())
            chat_task_ids = chat_hitl_task_ids.get(wid, set())
            missing_step_actions = step_ids - chat_step_ids
            missing_task_actions = hitl_ids - step_task_ids - chat_task_ids
            stats_map[wid]["hitl_tasks"] = len(hitl_ids)
            stats_map[wid]["pending_actions"] = (
                stats_map[wid]["chat_pending_actions"]
                + len(missing_step_actions)
                + len(missing_task_actions)
            )

    creator_map = await _workspace_creator_summaries(db, workspaces)
    result = []
    for ws in workspaces:
        resp = _to_response(ws, creator=creator_map.get(ws.id))
        data = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__.copy()
        data["stats"] = stats_map.get(ws.id, {})
        result.append(data)
    return result


@router.post("", response_model=WorkspaceResponse, status_code=201)
async def create_new_workspace(
    req: WorkspaceCreateRequest,
    _gate=Depends(require_plan("workspaces")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await create_workspace(
        db, user.entity_id,
        name=req.name, description=req.description,
        category=req.category, address=req.address,
        kind=req.kind, operating_context=req.operating_context,
        primary_work=req.primary_work,
        longitude=req.longitude,
        latitude=req.latitude,
        cover_image_url=req.cover_image_url,
        attribute_tags=req.attribute_tags,
        identity_label=req.identity_label,
        property_type=req.property_type,
        occupancy_status=req.occupancy_status,
        pms_property_id=req.pms_property_id,
        pms_unit_id=req.pms_unit_id,
        heartbeat_enabled=req.heartbeat_enabled,
        heartbeat_cadence=req.heartbeat_cadence,
    )
    settings = settings_with_default_workspace_access(ws.settings)
    settings.setdefault("created_by_user_id", user.id)
    ws.settings = settings
    # Enroll the creator as the workspace owner so they retain management
    # rights (settings, staff) even when they are not a firm-level
    # owner/admin. Without this, _require_workspace_manage would lock a
    # non-admin creator out of the workspace they just made.
    staff_id = await _ensure_user_staff_id(db, user)
    db.add(WorkspaceStaff(
        workspace_id=ws.id,
        staff_id=staff_id,
        user_id=user.id,
        role="owner",
        added_by=user.id,
        added_at=datetime.now(UTC),
        status="active",
    ))
    # flush (not commit) so the membership row persists within the same
    # request transaction that get_db commits at the end — adding a
    # mid-handler commit splits the txn and breaks downstream runtime sync.
    await db.flush()
    try:
        await sync_workspace_runtime_schedules(db, ws)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    from packages.core.services.workspace_service import record_activity

    await record_activity(
        db,
        ws.id,
        user.entity_id,
        event_type="workspace.created",
        summary="Workspace created",
        details={"fields": ["name", "description", "category"]},
        user_id=user.id,
    )
    from packages.core.services.plan_gate import invalidate_gate_cache

    invalidate_gate_cache(user.entity_id)
    await db.refresh(ws)
    return _to_response(ws, creator=user)


# ── Trash / Restore ─────────────────────────────────────────────────────────

@router.get("/trash/list", response_model=list[WorkspaceResponse])
async def list_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspaces in the soft-delete grace window. Returns rows with
    ``deleted_at`` set; consumers can compute days-until-purge from
    that timestamp + ``WORKSPACE_PURGE_GRACE_DAYS``."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only owner/admin can view workspace trash")
    rows = await list_trashed_workspaces(db, user.entity_id)
    creator_map = await _workspace_creator_summaries(db, rows)
    return [_to_response(ws, creator=creator_map.get(ws.id)) for ws in rows]


@router.get("/trash/grace-days")
async def get_grace_days():
    """Surface the configured grace window so the UI can render
    accurate "X days until permanent deletion" copy."""
    return {"grace_days": WORKSPACE_PURGE_GRACE_DAYS}


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await _require_workspace_read(db, workspace_id, user)
    creator_map = await _workspace_creator_summaries(db, [ws])
    return _to_response(ws, creator=creator_map.get(ws.id))


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_one_workspace(
    workspace_id: str,
    req: WorkspaceUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    heartbeat_touched = (
        "heartbeat_enabled" in req.model_fields_set
        or "heartbeat_cadence" in req.model_fields_set
    )
    update_fields = req.model_dump(exclude_none=True)
    heartbeat_payload: dict[str, Any] = {}
    if "heartbeat_enabled" in req.model_fields_set:
        heartbeat_payload["enabled"] = bool(req.heartbeat_enabled)
        update_fields.pop("heartbeat_enabled", None)
    if "heartbeat_cadence" in req.model_fields_set:
        heartbeat_payload["cadence"] = req.heartbeat_cadence
        update_fields.pop("heartbeat_cadence", None)

    audit_fields = sorted(set(update_fields.keys()) | set(heartbeat_payload.keys()))
    ws = await update_workspace(db, workspace_id, user.entity_id, **update_fields)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if heartbeat_touched:
        await _apply_workspace_operation_patches(
            db,
            workspace_id=workspace_id,
            entity_id=user.entity_id,
            user_id=user.id,
            source_event_id="api_workspace_heartbeat_update",
            patches=[{"op": "heartbeat_policy.update", "payload": heartbeat_payload}],
        )
        ws = await _require_workspace(db, workspace_id, user.entity_id)
    if audit_fields:
        from packages.core.services.workspace_service import record_activity

        await record_activity(
            db,
            workspace_id,
            user.entity_id,
            event_type="workspace.updated",
            summary="Workspace details updated",
            details={"fields": audit_fields},
            user_id=user.id,
        )
    _invalidate_workspace_context(workspace_id)
    creator_map = await _workspace_creator_summaries(db, [ws])
    return _to_response(ws, creator=creator_map.get(ws.id))


@router.delete("/{workspace_id}", status_code=204)
async def delete_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a workspace. The workspace remains restorable for
    ``WORKSPACE_PURGE_GRACE_DAYS`` days; after that the nightly
    ``ops.purge_soft_deleted_workspaces`` task hard-deletes it."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only owner/admin can delete workspaces")
    ok = await soft_delete_workspace(db, workspace_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Workspace not found")
    from packages.core.services.plan_gate import invalidate_gate_cache

    await db.commit()
    invalidate_gate_cache(user.entity_id)
    _invalidate_workspace_context(workspace_id)


@router.post("/{workspace_id}/restore", response_model=WorkspaceResponse)
async def restore_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recover a soft-deleted workspace before the grace window
    expires. After hard-purge there's nothing to restore."""
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only owner/admin can restore workspaces")
    from packages.core.services.plan_gate import check as check_plan_gate

    gate = await check_plan_gate(db, user.entity_id, "workspaces")
    if not gate.allowed:
        raise HTTPException(
            402,
            detail={
                "message": gate.message,
                "limit": gate.limit,
                "current": gate.current,
                "plan": gate.plan,
                "kind": "workspaces",
            },
        )
    ws = await restore_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(
            404,
            "Workspace not found in trash (already purged or never deleted)",
        )
    from packages.core.services.plan_gate import invalidate_gate_cache

    invalidate_gate_cache(user.entity_id)
    _invalidate_workspace_context(workspace_id)
    creator_map = await _workspace_creator_summaries(db, [ws])
    return _to_response(ws, creator=creator_map.get(ws.id))


# ── Pause / Resume ──────────────────────────────────────────────────────────

@router.post("/{workspace_id}/pause")
async def pause_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pause a workspace — stops strategist, dispatcher, and heartbeat."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    ws.status = "paused"
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_workspace_pause",
        patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": False}}],
    )
    return {"status": "paused", "workspace_id": workspace_id}


@router.post("/{workspace_id}/resume")
async def resume_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resume a paused workspace — re-enables strategist and heartbeat."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    ws.status = "active"
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_workspace_resume",
        patches=[{
            "op": "heartbeat_policy.update",
            "payload": {"enabled": True, "cadence": ws.heartbeat_cadence or "daily"},
        }],
    )
    return {"status": "active", "workspace_id": workspace_id}


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/dashboard")
async def workspace_dashboard(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspace-scoped dashboard — task stats, documents, agents, recent tasks."""
    await _require_workspace_read(db, workspace_id, user)
    stats = await get_workspace_stats(
        db,
        user.entity_id,
        workspace_id,
        timezone_name=user.timezone,
    )
    stats["custom_field_summary"] = await get_workspace_custom_field_summary(
        db, user.entity_id, workspace_id,
    )
    return stats


# ── Operating model ──────────────────────────────────────────────────────────

@router.get("/{workspace_id}/operating-model")
async def get_operating_model(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await _require_workspace_read(db, workspace_id, user)
    return {"workspace_id": ws.id, "operating_model": ws.operating_model}


@router.put("/{workspace_id}/operating-model")
async def update_full_operating_model(
    workspace_id: str,
    operating_model: dict[str, Any],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_operating_model_update",
        patches=[{"op": "operating_model.replace", "payload": {"operating_model": operating_model}}],
    )
    ws = await _require_workspace_manage(db, workspace_id, user)
    await db.commit()
    return ws


# ── Operation draft runtime ─────────────────────────────────────────────────

@router.get("/{workspace_id}/operation/current")
async def get_workspace_operation_current(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import get_current_operation_state

    state = await get_current_operation_state(db, workspace_id, user.entity_id)
    if state is None:
        raise HTTPException(404, "Workspace not found")
    return {"workspace_id": workspace_id, "state": state}


@router.post("/{workspace_id}/operation/drafts")
async def create_workspace_operation_draft(
    workspace_id: str,
    req: OperationDraftCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        create_operation_draft,
        draft_to_dict,
    )

    draft = await create_operation_draft(
        db,
        workspace_id,
        user.entity_id,
        user_id=user.id,
        source_event_id=req.source_event_id,
        initial_patches=req.patches,
    )
    if draft is None:
        raise HTTPException(404, "Workspace not found")
    payload = draft_to_dict(draft)
    await db.commit()
    return payload


@router.patch("/{workspace_id}/operation/drafts/{draft_id}")
async def patch_workspace_operation_draft(
    workspace_id: str,
    draft_id: str,
    req: OperationPatchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        draft_to_dict,
        patch_operation_draft,
    )

    try:
        draft = await patch_operation_draft(
            db,
            draft_id,
            user.entity_id,
            workspace_id,
            req.patches,
        )
    except OperationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    payload = draft_to_dict(draft)
    await db.commit()
    return payload


@router.post("/{workspace_id}/operation/drafts/{draft_id}/validate")
async def validate_workspace_operation_draft(
    workspace_id: str,
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        get_operation_draft,
        validate_operation_draft,
    )

    draft = await get_operation_draft(db, draft_id, user.entity_id, workspace_id)
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    validation = await validate_operation_draft(db, draft)
    await db.commit()
    return validation


@router.get("/{workspace_id}/operation/drafts/{draft_id}/diff")
async def diff_workspace_operation_draft(
    workspace_id: str,
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        get_operation_draft,
        preview_operation_diff,
    )

    draft = await get_operation_draft(db, draft_id, user.entity_id, workspace_id)
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    diff = await preview_operation_diff(db, draft)
    await db.commit()
    return diff


@router.post("/{workspace_id}/operation/drafts/{draft_id}/apply")
async def apply_workspace_operation_draft(
    workspace_id: str,
    draft_id: str,
    req: OperationApplyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        OperationConflictError,
        OperationValidationError,
        apply_operation_draft,
    )

    try:
        result = await apply_operation_draft(
            db,
            draft_id,
            user.entity_id,
            workspace_id,
            user_id=user.id,
            user_confirmation=req.user_confirmation,
        )
    except OperationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OperationValidationError as exc:
        raise HTTPException(400, exc.validation) from exc
    if result is None:
        raise HTTPException(404, "Operation draft not found")
    await db.commit()
    return result


@router.post("/{workspace_id}/operation/drafts/{draft_id}/discard")
async def discard_workspace_operation_draft(
    workspace_id: str,
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import (
        discard_operation_draft,
        draft_to_dict,
    )

    draft = await discard_operation_draft(
        db,
        draft_id,
        user.entity_id,
        workspace_id,
        user_id=user.id,
    )
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    payload = draft_to_dict(draft)
    await db.commit()
    return payload


@router.post("/{workspace_id}/operation/repair")
async def repair_workspace_operation_runtime_endpoint(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.workspace_operation_service import repair_workspace_operation_runtime

    result = await repair_workspace_operation_runtime(
        db,
        workspace_id,
        user.entity_id,
        user_id=user.id,
    )
    if result is None:
        raise HTTPException(404, "Workspace not found")
    await db.commit()
    return result


# ── Services ─────────────────────────────────────────────────────────────────

@router.post("/{workspace_id}/services")
async def add_service(
    workspace_id: str,
    req: ServiceRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_service_add",
        patches=[{
            "op": "service_role.upsert",
            "payload": {"key": req.key, "name": req.name, "description": req.description, "config": req.config},
        }],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return ws


@router.delete("/{workspace_id}/services/{service_key}", status_code=200)
async def remove_service(
    workspace_id: str,
    service_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_service_remove",
        patches=[{"op": "service_role.remove", "payload": {"key": service_key}}],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return ws


# ── Agent mappings ───────────────────────────────────────────────────────────

@router.get("/{workspace_id}/agents")
async def list_agent_mappings(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.workspace_service import get_workspace_agent_mappings
    await _require_workspace_manage(db, workspace_id, user)
    return await get_workspace_agent_mappings(db, workspace_id, user.entity_id)


@router.post("/{workspace_id}/agents")
async def map_agent(
    workspace_id: str,
    req: AgentMappingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    result = await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_agent_map",
        patches=[{
            "op": "agent_mapping.upsert",
            "payload": {
                "mapping": {
                    "service_key": req.service_key,
                    "agent_id": req.agent_id,
                    "custom_prompt": req.custom_prompt,
                },
            },
        }],
    )
    await db.commit()
    return result


@router.delete("/{workspace_id}/agents/{service_key}", status_code=200)
async def unmap_agent(
    workspace_id: str,
    service_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    result = await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_agent_unmap",
        patches=[{"op": "agent_mapping.remove", "payload": {"service_key": service_key}}],
    )
    await db.commit()
    return result


@router.get("/{workspace_id}/capabilities")
async def list_workspace_capabilities(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List tool/skill/MCP capabilities available to this workspace's agents."""
    from packages.core.ai.runtime import runtime_workspace_capability_tool_groups
    from packages.core.models.document import Integration
    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.workspace import (
        Agent,
        AgentSubscription,
        AgentToolBinding,
        ToolDefinition,
    )

    ws = await _require_workspace_read(db, workspace_id, user)
    runtime_tool_groups = runtime_workspace_capability_tool_groups()
    always_runtime_tools = list(runtime_tool_groups["always"])
    contextual_runtime_tools = list(runtime_tool_groups["contextual"])
    subs = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all()
    agent_ids = [s.agent_id for s in subs if s.agent_id]

    agent_map: dict[str, Agent] = {}
    if agent_ids:
        agents = (await db.execute(
            select(Agent).where(
                Agent.id.in_(agent_ids),
                Agent.deleted_at.is_(None),
                or_(Agent.entity_id == user.entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all()
        agent_map = {a.id: a for a in agents}

    tools_by_agent: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in agent_ids}
    if agent_ids:
        tool_rows = (await db.execute(
            select(AgentToolBinding.agent_id, ToolDefinition).join(
                ToolDefinition,
                ToolDefinition.id == AgentToolBinding.tool_id,
            ).where(
                AgentToolBinding.agent_id.in_(agent_ids),
                ToolDefinition.status == "active",
            )
        )).all()
        for agent_id, tool in tool_rows:
            tools_by_agent.setdefault(agent_id, []).append({
                "id": tool.id,
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "category": tool.category,
            })

    skills_by_agent: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in agent_ids}
    if agent_ids:
        skill_rows = (await db.execute(
            select(AgentSkillBinding.agent_id, Skill).join(
                Skill,
                Skill.id == AgentSkillBinding.skill_id,
            ).where(
                AgentSkillBinding.agent_id.in_(agent_ids),
                AgentSkillBinding.status == "active",
                Skill.status == "active",
                or_(Skill.entity_id == user.entity_id, Skill.is_public.is_(True)),
            )
        )).all()
        for agent_id, skill in skill_rows:
            skills_by_agent.setdefault(agent_id, []).append({
                "id": skill.id,
                "slug": skill.slug,
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "category": skill.category,
                "scope": "entity" if skill.entity_id == user.entity_id else "public",
                "tools": list(skill.tools or []),
            })

    integration_counts: dict[str, int] = {}
    integration_rows = (await db.execute(
        select(Integration.provider).where(
            Integration.entity_id == user.entity_id,
            Integration.status == "active",
        )
    )).scalars().all()
    for provider in integration_rows:
        key = canonical_provider_key(provider)
        integration_counts[key] = integration_counts.get(key, 0) + 1

    from packages.core.models.user import OAuthAccount

    oauth_rows = (await db.execute(
        select(OAuthAccount.provider).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.access_token.is_not(None),
        )
    )).scalars().all()
    for provider in oauth_rows:
        key = canonical_provider_key(provider)
        integration_counts[key] = integration_counts.get(key, 0) + 1

    mcp_by_agent: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in agent_ids}
    if agent_ids:
        mcp_rows = (await db.execute(
            select(AgentMCPBinding.agent_id, AgentMCPBinding, MCPServer).join(
                MCPServer,
                MCPServer.id == AgentMCPBinding.mcp_server_id,
            ).where(
                AgentMCPBinding.agent_id.in_(agent_ids),
                AgentMCPBinding.status == "active",
                MCPServer.status == "active",
            )
        )).all()
        for agent_id, binding, server in mcp_rows:
            account_count = integration_counts.get(canonical_provider_key(server.server_key), 0)
            mcp_by_agent.setdefault(agent_id, []).append({
                "binding_id": binding.id,
                "server_id": server.id,
                "server_key": server.server_key,
                "name": server.name,
                "description": server.description,
                "auth_type": server.auth_type,
                "allowed_tools": binding.allowed_tools,
                "ready": account_count > 0 or server.auth_type == "none",
                "connected_accounts": account_count,
            })

    from packages.core.services.integration_resolution import resolve_missing_integration_flags

    flagged = await resolve_missing_integration_flags(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        flagged=list((ws.settings or {}).get("flagged_integrations") or []),
    )
    flagged_by_service: dict[str, list[dict[str, Any]]] = {}
    workspace_wide_flags: list[dict[str, Any]] = []
    for flag in flagged:
        if not isinstance(flag, dict):
            continue
        service_keys = list(flag.get("linked_service_keys") or [])
        clean = {
            "provider": flag.get("provider"),
            "purpose": flag.get("purpose", ""),
            "required": bool(flag.get("required", True)),
            "source": flag.get("source", ""),
        }
        if service_keys:
            for service_key in service_keys:
                flagged_by_service.setdefault(service_key, []).append(clean)
        else:
            workspace_wide_flags.append(clean)

    services = []
    for sub in subs:
        agent = agent_map.get(sub.agent_id)
        service_key = sub.service_key or ""
        services.append({
            "agent_subscription_id": sub.id,
            "service_key": service_key,
            "agent_id": sub.agent_id,
            "agent": {
                "id": agent.id,
                "name": getattr(agent, "display_name", None) or agent.name,
                "avatar_url": getattr(agent, "avatar_url", None),
                "category": agent.category,
            } if agent else None,
            "custom_prompt": sub.custom_prompt,
            "tools": tools_by_agent.get(sub.agent_id, []),
            "skills": skills_by_agent.get(sub.agent_id, []),
            "integrations": mcp_by_agent.get(sub.agent_id, []),
            "missing_integrations": flagged_by_service.get(service_key, []),
        })

    return {
        "workspace_id": workspace_id,
        "workspace_runtime_tools": always_runtime_tools,
        "workspace_contextual_tools": contextual_runtime_tools,
        "services": services,
        "workspace_missing_integrations": workspace_wide_flags,
    }


# ── Goals & rules ────────────────────────────────────────────────────────────

@router.put("/{workspace_id}/goals")
async def update_goals(
    workspace_id: str,
    req: GoalsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_goals_update",
        patches=[{"op": "goals.replace", "payload": {"goals": req.goals}}],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return ws


@router.put("/{workspace_id}/rules")
async def update_rules(
    workspace_id: str,
    req: RulesRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_rules_update",
        patches=[{"op": "rules.replace", "payload": {"rules": req.rules}}],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return ws


# ── Activity ─────────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/activity")
async def list_workspace_activity(
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    event_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.workspace_service import list_activity
    await _require_workspace_read(db, workspace_id, user)
    return await list_activity(db, workspace_id, user.entity_id, limit=limit, event_type=event_type)


# ── Runtime evidence / agent learning ───────────────────────────────────────

def _normalized_evidence_status_and_summary(row) -> tuple[str, str]:
    """Normalize legacy runtime evidence before it reaches the UI.

    Older supervisor runs could store a task_run as succeeded even when the
    underlying plan failed with no successful steps. The recorder now prevents
    that; this keeps historical rows from teaching or displaying the wrong
    signal.
    """
    status = str(row.status or "partial")
    summary = str(row.summary or "")
    details = row.details or {}
    metrics = row.metrics or {}
    if (
        row.evidence_type == "task_run"
        and status == "succeeded"
        and str(details.get("plan_status") or "").lower() in {"failed", "error"}
        and int(metrics.get("failed_steps") or 0) > 0
    ):
        if int(metrics.get("done_steps") or 0) == 0:
            status = "failed"
            summary = summary.replace("Task completed:", "Task failed:", 1)
            summary = summary.replace("Task succeeded:", "Task failed:", 1)
        elif int(metrics.get("artifact_count") or 0) <= 0:
            status = "partial"
            summary = summary.replace("Task completed:", "Task partial:", 1)
            summary = summary.replace("Task succeeded:", "Task partial:", 1)
    if (
        row.evidence_type == "task_run"
        and status in {"blocked", "partial"}
        and str(details.get("plan_status") or "").lower() in {"completed", "done", "succeeded", "success"}
        and str(details.get("task_status") or "").lower()
        in {"waiting_on_customer", "waiting_human", "blocked", "paused", "needs_attention"}
        and int(metrics.get("failed_steps") or 0) == 0
        and int(metrics.get("blocked_steps") or 0) == 0
        and int(metrics.get("done_steps") or 0) > 0
        and (int(metrics.get("artifact_count") or 0) > 0 or _runtime_evidence_has_artifact_hint(details))
    ):
        status = "succeeded"
        title = str(details.get("task_title") or "").strip()
        summary = f"Task completed: {title}" if title else summary
    return status, summary


def _runtime_evidence_has_artifact_hint(details: dict) -> bool:
    for step in details.get("steps") or []:
        if not isinstance(step, dict):
            continue
        text = str(step.get("result_excerpt") or "").lower()
        if any(marker in text for marker in ('"files"', '"artifacts"', "fs_path", ".md", ".pdf", ".docx", ".pptx", ".xlsx")):
            return True
    text = str(details.get("actual_output_excerpt") or "").lower()
    return any(marker in text for marker in ('"files"', '"artifacts"', "fs_path", ".md", ".pdf", ".docx", ".pptx", ".xlsx"))


def _runtime_evidence_response(row) -> RuntimeEvidenceResponse:
    status, summary = _normalized_evidence_status_and_summary(row)
    return RuntimeEvidenceResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        task_id=row.task_id,
        trace_id=row.trace_id,
        evidence_type=row.evidence_type,
        source=row.source,
        status=status,
        summary=summary,
        details=row.details or {},
        metrics=row.metrics or {},
        created_at=row.created_at,
    )


def _learning_candidate_response(row) -> LearningCandidateResponse:
    return LearningCandidateResponse(
        id=row.id,
        workspace_id=row.workspace_id,
        agent_id=row.agent_id,
        user_id=row.user_id,
        candidate_type=row.candidate_type,
        scope=row.scope,
        title=row.title,
        summary=row.summary,
        payload=row.payload or {},
        evidence_ids=list(row.evidence_ids or []),
        risk_level=row.risk_level,
        status=row.status,
        confidence=float(row.confidence or 0),
        created_by=row.created_by,
        resolution=row.resolution or {},
        applied_at=row.applied_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/{workspace_id}/runtime/evidence", response_model=list[RuntimeEvidenceResponse])
async def list_workspace_runtime_evidence(
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    evidence_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.runtime_learning import list_runtime_evidence

    await _require_workspace_read(db, workspace_id, user)
    rows = await list_runtime_evidence(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        evidence_type=evidence_type,
        status=status,
        limit=limit,
    )
    return [_runtime_evidence_response(row) for row in rows]


@router.get("/{workspace_id}/learning-candidates", response_model=list[LearningCandidateResponse])
async def list_workspace_learning_candidates(
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default="proposed"),
    candidate_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.runtime_learning import list_learning_candidates

    await _require_workspace_read(db, workspace_id, user)
    status_filter = (status or "").strip()
    if status_filter.lower() in {"", "all", "*"}:
        status_filter = None
    rows = await list_learning_candidates(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        status=status_filter,
        candidate_type=candidate_type,
        limit=limit,
    )
    return [_learning_candidate_response(row) for row in rows]


@router.post("/{workspace_id}/learning-candidates/{candidate_id}/resolve", response_model=LearningCandidateResponse)
async def resolve_workspace_learning_candidate(
    workspace_id: str,
    candidate_id: str,
    req: LearningCandidateResolveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.runtime_learning import resolve_learning_candidate

    await _require_workspace_manage(db, workspace_id, user)
    row = await resolve_learning_candidate(
        db,
        entity_id=user.entity_id,
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        status=req.status,
        user_id=user.id,
        note=req.note,
    )
    if not row:
        raise HTTPException(404, "Learning candidate not found")
    await db.commit()
    await db.refresh(row)
    return _learning_candidate_response(row)


@router.post("/{workspace_id}/learning-candidates/{candidate_id}/apply", response_model=LearningCandidateResponse)
async def apply_workspace_learning_candidate(
    workspace_id: str,
    candidate_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.services.runtime_learning import apply_learning_candidate

    await _require_workspace_manage(db, workspace_id, user)
    try:
        row = await apply_learning_candidate(
            db,
            entity_id=user.entity_id,
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not row:
        raise HTTPException(404, "Learning candidate not found")
    await db.commit()
    await db.refresh(row)
    if row.status != "applied":
        from packages.core.services.runtime_learning import enqueue_learning_candidate_apply

        failed_row = await enqueue_learning_candidate_apply(
            db,
            entity_id=user.entity_id,
            candidate_id=candidate_id,
            workspace_id=workspace_id,
            user_id=user.id,
        )
        if failed_row:
            await db.commit()
            row = failed_row
            await db.refresh(row)
    return _learning_candidate_response(row)


# ── Setup (conversational workspace configuration) ──────────────────────────

@router.post("/{workspace_id}/setup/turn")
async def setup_turn(
    workspace_id: str,
    req: SetupTurnRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Process one turn of the workspace setup conversation."""
    raise HTTPException(
        status_code=410,
        detail="Workspace setup has moved to /api/v1/workspace-drafts.",
    )


@router.post("/{workspace_id}/setup/finalize")
async def setup_finalize(
    workspace_id: str,
    req: SetupFinalizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Finalize setup — generate and persist the operating model."""
    raise HTTPException(
        status_code=410,
        detail="Workspace setup has moved to /api/v1/workspace-drafts.",
    )


# ── Staff management ─────────────────────────────────────────────────────

@router.get("/{workspace_id}/staff", response_model=list[StaffResponse])
async def list_workspace_staff(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List staff assigned to a workspace."""
    from packages.core.models.staff import Staff

    await _require_workspace_read(db, workspace_id, user)
    result = await db.execute(
        select(WorkspaceStaff)
        .join(Workspace, Workspace.id == WorkspaceStaff.workspace_id)
        .join(Staff, Staff.id == WorkspaceStaff.staff_id)
        .where(
            WorkspaceStaff.workspace_id == workspace_id,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
            Staff.entity_id == user.entity_id,
            Staff.deleted_at.is_(None),
            WorkspaceStaff.status == "active",
        )
    )
    rows = result.scalars().all()
    return [
        StaffResponse(
            id=r.id, workspace_id=r.workspace_id,
            staff_id=r.staff_id, user_id=getattr(r, "user_id", None),
            role=r.role,
            added_by=getattr(r, "added_by", None),
            added_at=getattr(r, "added_at", None),
            expires_at=getattr(r, "expires_at", None),
            status=getattr(r, "status", None),
            created_at=r.created_at,
        )
        for r in rows
    ]


# Workspace-role enum from RFC §5.2. Empty / None / legacy values are
# accepted for backwards compatibility but new code should send one of
# these.
_WORKSPACE_ROLES = {"owner", "editor", "contributor", "viewer", "external_client"}


@router.post("/{workspace_id}/staff", response_model=StaffResponse, status_code=201)
async def assign_staff(
    workspace_id: str,
    req: StaffAssignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Assign a staff member to a workspace."""
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.models.staff import Staff
    staff = (await db.execute(
        select(Staff).where(
            Staff.id == req.staff_id,
            Staff.entity_id == user.entity_id,
            Staff.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not staff:
        raise HTTPException(404, "Staff member not found")
    # Permission-v1 user-scoped assignments should use the canonical
    # workspace role enum. Legacy staff-scoped assignments historically used
    # business labels like "reviewer" / "lead"; keep those working.
    if req.user_id is not None and req.role and req.role not in _WORKSPACE_ROLES:
        raise HTTPException(
            400, f"Invalid workspace role: {req.role}. "
            f"Expected one of {sorted(_WORKSPACE_ROLES)}"
        )
    ws_staff = (await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.staff_id == req.staff_id,
        ).limit(1)
    )).scalar_one_or_none()
    was_existing = ws_staff is not None
    if ws_staff:
        ws_staff.role = req.role
        if req.expires_at is not None:
            ws_staff.expires_at = req.expires_at
        if req.user_id is not None:
            ws_staff.user_id = req.user_id
        # Reactivate if previously inactive
        if getattr(ws_staff, "status", None) != "active":
            ws_staff.status = "active"
    else:
        ws_staff = WorkspaceStaff(
            workspace_id=workspace_id,
            staff_id=req.staff_id,
            user_id=req.user_id or staff.user_id,
            role=req.role,
            expires_at=req.expires_at,
            added_by=user.id,
            added_at=datetime.now(UTC),
            status="active",
        )
        db.add(ws_staff)
    from packages.core.services.workspace_service import record_activity

    await record_activity(
        db,
        workspace_id,
        user.entity_id,
        event_type="workspace.member_updated" if was_existing else "workspace.member_added",
        summary="Workspace member updated" if was_existing else "Workspace member added",
        details={
            "staff_id": req.staff_id,
            "user_id": req.user_id or getattr(staff, "user_id", None),
            "role": req.role,
            "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        },
        user_id=user.id,
    )
    await db.commit()
    await _mark_workspace_staff_changed(user.entity_id, workspace_id)
    await db.refresh(ws_staff)
    return StaffResponse(
        id=ws_staff.id, workspace_id=ws_staff.workspace_id,
        staff_id=ws_staff.staff_id,
        user_id=getattr(ws_staff, "user_id", None),
        role=ws_staff.role,
        added_by=getattr(ws_staff, "added_by", None),
        added_at=getattr(ws_staff, "added_at", None),
        expires_at=getattr(ws_staff, "expires_at", None),
        status=getattr(ws_staff, "status", None),
        created_at=ws_staff.created_at,
    )


@router.delete("/{workspace_id}/staff/{staff_id}", status_code=204)
async def remove_staff(
    workspace_id: str,
    staff_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a staff member from a workspace."""
    await _require_workspace_manage(db, workspace_id, user)
    target = (await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.staff_id == staff_id,
        )
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Staff assignment not found")
    from packages.core.services.workspace_service import record_activity

    await record_activity(
        db,
        workspace_id,
        user.entity_id,
        event_type="workspace.member_removed",
        summary="Workspace member removed",
        details={
            "staff_id": staff_id,
            "user_id": getattr(target, "user_id", None),
            "role": getattr(target, "role", None),
        },
        user_id=user.id,
    )
    await db.delete(target)
    await db.commit()
    await _mark_workspace_staff_changed(user.entity_id, workspace_id)


# ── Heartbeat ────────────────────────────────────────────────────────────

@router.post("/{workspace_id}/heartbeat/enable")
async def enable_heartbeat(
    workspace_id: str,
    cadence: str = Query(default="daily"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable heartbeat for a workspace."""
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_heartbeat_enable",
        patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": True, "cadence": cadence}}],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return {"workspace_id": ws.id, "heartbeat_enabled": True, "heartbeat_cadence": cadence}


@router.post("/{workspace_id}/heartbeat/disable")
async def disable_heartbeat(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disable heartbeat for a workspace."""
    await _require_workspace_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_heartbeat_disable",
        patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": False}}],
    )
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    await db.commit()
    return {"workspace_id": ws.id, "heartbeat_enabled": False}


@router.get("/{workspace_id}/heartbeat/status")
async def heartbeat_status(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get heartbeat status for a workspace."""
    ws = await _require_workspace_read(db, workspace_id, user)
    enabled = ws.heartbeat_enabled or False
    return {
        "workspace_id": ws.id,
        "heartbeat_enabled": enabled,
        "enabled": enabled,
        "heartbeat_cadence": ws.heartbeat_cadence,
        "last_heartbeat_at": ws.last_heartbeat_at.isoformat() if ws.last_heartbeat_at else None,
    }


# ── Channels scoped to workspace ────────────────────────────────────────

@router.get("/{workspace_id}/channels")
async def list_workspace_channels(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List channel configurations for a workspace, with agent binding info."""
    from packages.core.models.document import Channel
    from packages.core.models.workspace import Agent

    # Load all Channel bindings for this workspace.
    # Wrapped in try/except because the agent_subscription_id column
    # may not exist yet (migration 20260427_03 adds it).
    binding_by_cc: dict[str, Channel] = {}
    agent_map: dict[str, dict] = {}
    cc_ids: set[str] = set()
    await _require_workspace_read(db, workspace_id, user)
    try:
        bindings = (await db.execute(
            select(Channel).where(
                Channel.workspace_id == workspace_id,
                Channel.entity_id == user.entity_id,
                Channel.status == "active",
            )
        )).scalars().all()
        for b in bindings:
            cc_id = (b.config or {}).get("channel_config_id")
            if cc_id:
                cc_ids.add(cc_id)
                binding_by_cc[cc_id] = b

        agent_ids = {b.agent_id for b in bindings if b.agent_id}
        if agent_ids:
            agents = (await db.execute(
                select(Agent).where(
                    Agent.id.in_(agent_ids),
                    Agent.deleted_at.is_(None),
                    or_(Agent.entity_id == user.entity_id, Agent.entity_id.is_(None)),
                )
            )).scalars().all()
            for a in agents:
                agent_map[a.id] = {
                    "id": a.id,
                    "name": getattr(a, "display_name", None) or a.name,
                    "avatar_url": getattr(a, "avatar_url", None),
                }
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to load workspace channel bindings for %s: %s",
            workspace_id,
            exc,
            exc_info=True,
        )

    # Include both workspace-scoped ChannelConfigs and shared/global configs
    # that this workspace has explicitly bound through a Channel row.
    channel_config_filters = [ChannelConfig.workspace_id == workspace_id]
    if cc_ids:
        channel_config_filters.append(ChannelConfig.id.in_(cc_ids))

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == user.entity_id,
            or_(*channel_config_filters),
        )
    )
    rows = result.scalars().all()

    out = []
    for ch in rows:
        binding = binding_by_cc.get(ch.id)
        bound_agent = None
        if binding and binding.agent_id:
            bound_agent = agent_map.get(binding.agent_id)
        binding_config = dict(binding.config or {}) if binding else {}
        merged_config = dict(ch.config or {})
        merged_config.update({
            k: v for k, v in binding_config.items()
            if k != "channel_config_id"
        })
        merged_config["language"] = _normalize_channel_language(
            merged_config.get("language") or merged_config.get("locale")
        )

        entry: dict[str, Any] = {
            "id": ch.id,
            "entity_id": ch.entity_id,
            "workspace_id": ch.workspace_id,
            "channel_type": ch.channel_type,
            "provider": ch.provider,
            "name": ch.name,
            "config": merged_config,
            "created_at": ch.created_at.isoformat() if ch.created_at else None,
            "bound_agent": bound_agent,
            "channel_binding_id": binding.id if binding else None,
            "source_scope": "workspace" if ch.workspace_id == workspace_id else "shared",
        }

        # Webchat channels: include public_token for QR/link
        if ch.channel_type == "webchat":
            entry["public_token"] = merged_config.get("public_token")

        out.append(entry)

    return out


@router.get("/{workspace_id}/channels/available")
async def list_available_workspace_channels(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List existing ChannelConfigs that can be attached to this workspace."""
    from packages.core.models.document import Channel

    await _require_workspace_manage(db, workspace_id, user)
    bindings = (await db.execute(
        select(Channel).where(
            Channel.workspace_id == workspace_id,
            Channel.entity_id == user.entity_id,
            Channel.status == "active",
        )
    )).scalars().all()
    attached_cc_ids = {
        (b.config or {}).get("channel_config_id")
        for b in bindings
        if (b.config or {}).get("channel_config_id")
    }
    rows = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.entity_id == user.entity_id,
            ChannelConfig.status == "active",
            or_(
                ChannelConfig.workspace_id.is_(None),
                ChannelConfig.workspace_id == workspace_id,
            ),
        )
    )).scalars().all()
    return [
        {
            "id": ch.id,
            "channel_type": ch.channel_type,
            "provider": ch.provider,
            "name": ch.name,
            "config": _normalized_channel_config(ch.config or {}),
            "workspace_id": ch.workspace_id,
            "attached": ch.id in attached_cc_ids,
            "source_scope": "workspace" if ch.workspace_id == workspace_id else "shared",
        }
        for ch in rows
    ]


@router.post("/{workspace_id}/channels", status_code=201)
async def attach_workspace_channel(
    workspace_id: str,
    req: WorkspaceChannelRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach an existing ChannelConfig to a workspace, or create webchat.

    ChannelConfig owns provider credentials. Channel owns the workspace routing
    binding to an AgentSubscription, so the same integration can serve multiple
    workspaces without duplicating secrets.
    """
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.workspace_service import record_activity

    await _require_workspace_manage(db, workspace_id, user)

    sub = None
    if req.agent_subscription_id:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.id == req.agent_subscription_id,
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
        if not sub:
            raise HTTPException(404, "Agent subscription not found")
    elif req.linked_service_key:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == req.linked_service_key,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
    elif req.agent_id:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == req.agent_id,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()

    channel_config = None
    if req.channel_config_id:
        channel_config = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == req.channel_config_id,
                ChannelConfig.entity_id == user.entity_id,
                ChannelConfig.status == "active",
                or_(
                    ChannelConfig.workspace_id.is_(None),
                    ChannelConfig.workspace_id == workspace_id,
                ),
            )
        )).scalar_one_or_none()
        if not channel_config:
            raise HTTPException(404, "Channel config not found")
    else:
        channel_type = (req.channel_type or "webchat").strip()
        if channel_type != "webchat":
            raise HTTPException(400, "channel_config_id is required for external channels")
        public_token = generate_ulid()
        cfg = _normalized_channel_config(req.config or {})
        cfg.update({
            "public_token": public_token,
            "role": req.role or "primary_external",
            "purpose": req.purpose or "Public web chat for this workspace.",
            "linked_service_key": req.linked_service_key or (sub.service_key if sub else ""),
            "login_required": bool(cfg.get("login_required", False)),
        })
        channel_config = ChannelConfig(
            id=generate_ulid(),
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            channel_type="webchat",
            provider="webchat",
            name=req.name or "Workspace webchat",
            config=cfg,
            credentials={},
            status="active",
        )
        db.add(channel_config)
        await db.flush()

    binding_config = {
        "channel_config_id": channel_config.id,
        "role": req.role or "primary_external",
        "purpose": req.purpose or (channel_config.config or {}).get("purpose") or "",
        "linked_service_key": req.linked_service_key or (sub.service_key if sub else ""),
    }
    if req.config:
        binding_config.update(_normalized_channel_config(req.config))
    existing = (await db.execute(
        select(Channel).where(
            Channel.entity_id == user.entity_id,
            Channel.workspace_id == workspace_id,
            Channel.config["channel_config_id"].astext == channel_config.id,
        )
    )).scalar_one_or_none()
    if existing:
        existing.agent_id = sub.agent_id if sub else req.agent_id
        existing.agent_subscription_id = sub.id if sub else None
        existing.name = req.name or existing.name or channel_config.name or channel_config.channel_type
        existing.config = binding_config
        existing.status = "active"
        channel_binding = existing
    else:
        channel_binding = Channel(
            id=generate_ulid(),
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            type=channel_config.channel_type,
            name=req.name or channel_config.name or channel_config.channel_type,
            agent_id=sub.agent_id if sub else req.agent_id,
            agent_subscription_id=sub.id if sub else None,
            config=binding_config,
            status="active",
        )
        db.add(channel_binding)

    await record_activity(
        db,
        workspace_id,
        user.entity_id,
        event_type="workspace.channel_attached",
        summary=f"Attached {channel_config.channel_type} channel",
        details={
            "channel_config_id": channel_config.id,
            "channel_binding_id": channel_binding.id,
            "channel_type": channel_config.channel_type,
            "linked_service_key": binding_config["linked_service_key"],
        },
        user_id=user.id,
        agent_id=sub.agent_id if sub else req.agent_id,
    )
    await db.commit()
    return {"channel_config_id": channel_config.id, "channel_binding_id": channel_binding.id}


@router.patch("/{workspace_id}/channels/{channel_binding_id}")
async def update_workspace_channel(
    workspace_id: str,
    channel_binding_id: str,
    req: WorkspaceChannelUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a workspace channel binding and workspace-owned channel config."""
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.workspace_service import record_activity

    await _require_workspace_manage(db, workspace_id, user)
    binding = (await db.execute(
        select(Channel).where(
            Channel.id == channel_binding_id,
            Channel.workspace_id == workspace_id,
            Channel.entity_id == user.entity_id,
            Channel.status == "active",
        )
    )).scalar_one_or_none()
    if not binding:
        raise HTTPException(404, "Channel binding not found")

    cc_id = (binding.config or {}).get("channel_config_id")
    if not cc_id:
        raise HTTPException(400, "Channel binding is missing channel_config_id")
    channel_config = (await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == cc_id,
            ChannelConfig.entity_id == user.entity_id,
            ChannelConfig.status == "active",
        )
    )).scalar_one_or_none()
    if not channel_config:
        raise HTTPException(404, "Channel config not found")

    routing_requested = (
        req.agent_subscription_id is not None
        or req.linked_service_key is not None
        or req.agent_id is not None
    )
    sub = None
    if req.agent_subscription_id:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.id == req.agent_subscription_id,
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
        if not sub:
            raise HTTPException(404, "Agent subscription not found")
    elif req.linked_service_key:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == req.linked_service_key,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()
    elif req.agent_id:
        sub = (await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == user.entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == req.agent_id,
                AgentSubscription.status == "active",
            )
        )).scalar_one_or_none()

    req_config = _normalized_channel_config(req.config) if req.config else {}
    binding_config = dict(binding.config or {})
    binding_config["channel_config_id"] = cc_id
    if req.role is not None:
        binding_config["role"] = req.role
    if req.purpose is not None:
        binding_config["purpose"] = req.purpose
    if req.linked_service_key is not None:
        binding_config["linked_service_key"] = req.linked_service_key
    if req_config:
        binding_config.update(req_config)

    if req.name is not None:
        binding.name = req.name.strip() or binding.name
    if routing_requested:
        binding.agent_id = sub.agent_id if sub else req.agent_id
        binding.agent_subscription_id = sub.id if sub else None
    binding.config = binding_config

    if channel_config.workspace_id == workspace_id:
        if req.name is not None:
            channel_config.name = req.name.strip() or channel_config.name
        cfg = dict(channel_config.config or {})
        if req.purpose is not None:
            cfg["purpose"] = req.purpose
        if req.role is not None:
            cfg["role"] = req.role
        if req.linked_service_key is not None:
            cfg["linked_service_key"] = req.linked_service_key
        if "login_required" in req_config:
            cfg["login_required"] = bool(req_config.get("login_required"))
        if "language" in req_config:
            cfg["language"] = _normalize_channel_language(req_config.get("language"))
        channel_config.config = cfg

    await record_activity(
        db,
        workspace_id,
        user.entity_id,
        event_type="workspace.channel_updated",
        summary=f"Updated {channel_config.channel_type} channel",
        details={
            "channel_config_id": channel_config.id,
            "channel_binding_id": binding.id,
            "channel_type": channel_config.channel_type,
            "linked_service_key": binding_config.get("linked_service_key"),
        },
        user_id=user.id,
        agent_id=(sub.agent_id if sub else req.agent_id) if routing_requested else binding.agent_id,
    )
    await db.commit()
    return {"channel_config_id": channel_config.id, "channel_binding_id": binding.id}


@router.delete("/{workspace_id}/channels/{channel_binding_id}", status_code=204)
async def remove_workspace_channel(
    workspace_id: str,
    channel_binding_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from packages.core.models.document import Channel

    await _require_workspace_manage(db, workspace_id, user)
    binding = (await db.execute(
        select(Channel).where(
            Channel.id == channel_binding_id,
            Channel.workspace_id == workspace_id,
            Channel.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if not binding:
        raise HTTPException(404, "Channel binding not found")
    cc_id = (binding.config or {}).get("channel_config_id")
    await db.delete(binding)
    if cc_id:
        cc = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == cc_id,
                ChannelConfig.entity_id == user.entity_id,
                ChannelConfig.workspace_id == workspace_id,
            )
        )).scalar_one_or_none()
        if cc:
            await db.delete(cc)
    await db.commit()


# ── Resolve flagged integrations ──────────────────────────────────────────

@router.post("/{workspace_id}/resolve-integrations")
async def resolve_flagged_integrations(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-check flagged integrations and activate what each flag represents.

    When a workspace is created, channels that need external integrations (email,
    twilio, etc.) are skipped if the provider isn't connected yet, and recorded
    in workspace.settings.flagged_integrations.  This endpoint re-checks which
    providers are now active, creates the missing ChannelConfig rows, binds them
    to the right AgentSubscription, and removes resolved entries from the flag list.

    Agent/tool integrations are also stored in flagged_integrations. Those should
    be marked resolved once connected, but must not become fake inbound channels
    such as "twitter_x".
    """
    from packages.core.models.document import Integration
    from packages.core.models.user import OAuthAccount
    from packages.core.models.workspace import AgentSubscription
    from packages.core.models.base import generate_ulid
    from packages.core.services.channels.base import registered_channel_types

    ws = await _require_workspace_manage(db, workspace_id, user)

    settings = dict(ws.settings or {})
    flagged = list(settings.get("flagged_integrations") or [])
    if not flagged:
        return {"resolved": [], "remaining": []}
    from packages.core.services.integration_resolution import (
        connected_integration_provider_keys,
        resolve_missing_integration_provider_key,
        supported_integration_provider_keys,
    )
    supported_providers = await supported_integration_provider_keys(db)

    # Fetch entity's currently active integration providers
    rows = (await db.execute(
        select(Integration.provider).where(
            Integration.entity_id == user.entity_id,
            Integration.status == "active",
        )
    )).scalars().all()
    oauth_rows = (await db.execute(
        select(OAuthAccount.provider).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.access_token.is_not(None),
        )
    )).scalars().all()
    active_providers = {
        canonical_provider_key(provider)
        for provider in [*rows, *oauth_rows]
    }
    active_providers.update(await connected_integration_provider_keys(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
    ))

    # Fetch agent subscriptions for this workspace (for channel→agent binding)
    subs = (await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all()
    sub_by_service: dict[str, AgentSubscription] = {}
    for s in subs:
        if s.service_key:
            sub_by_service[s.service_key] = s

    # Also check the operating model for channel specs we skipped
    om = ws.operating_model or {}
    cc = om.get("channel_config", {})
    # Build a lookup of channel specs from the original operating model
    channel_specs: list[dict] = []
    for ch in cc.get("channels", []):
        channel_specs.append(ch)
    if cc.get("primary_external_channel"):
        spec = dict(cc["primary_external_channel"])
        spec.setdefault("role", "primary_external")
        channel_specs.append(spec)
    if cc.get("internal_channel"):
        spec = dict(cc["internal_channel"])
        spec.setdefault("role", "internal")
        channel_specs.append(spec)
    for sec in cc.get("secondary_external_channels", []):
        spec = dict(sec)
        spec.setdefault("role", "secondary_external")
        channel_specs.append(spec)
    supported_channel_types = set(registered_channel_types())

    resolved = []
    remaining = []

    for flag in flagged:
        resolution = resolve_missing_integration_provider_key(
            (flag or {}).get("provider", ""),
            supported_provider_keys=supported_providers,
            connected_provider_keys=set(),
        )
        if resolution is None:
            continue
        if resolution.changed or resolution.covered_provider:
            flag = dict(flag or {})
            flag["provider"] = resolution.provider
            if resolution.covered_provider:
                flag["covered_provider"] = resolution.covered_provider
        provider = resolution.provider
        provider_key = canonical_provider_key(provider)
        if provider_key in active_providers:
            # Provider is now connected. Only channel_setup flags should create
            # ChannelConfig/Channel rows; agent_design/explicit flags just
            # unlock tools or MCP-backed capabilities.
            spec = next(
                (s for s in channel_specs
                 if provider_keys_match((s.get("provider") or s.get("channel_type", "")), provider)),
                None,
            )
            flag_source = str((flag or {}).get("source") or "")
            should_create_channel = bool(spec) or flag_source == "channel_setup"
            if not should_create_channel:
                resolved.append(provider)
                continue

            ch_type = (spec or {}).get("channel_type", provider) if spec else provider
            if ch_type not in supported_channel_types:
                remaining.append(flag)
                continue

            role = (spec or {}).get("role", "channel") if spec else "channel"
            linked_service_key = (spec or {}).get("linked_service_key", "") if spec else ""
            purpose = flag.get("purpose", "")

            # Check if channel already exists (avoid duplicates)
            existing = (await db.execute(
                select(ChannelConfig.id).where(
                    ChannelConfig.entity_id == user.entity_id,
                    ChannelConfig.workspace_id == workspace_id,
                    ChannelConfig.provider.in_(provider_key_aliases(provider)),
                )
            )).scalar_one_or_none()

            if not existing:
                from packages.core.models.document import Channel

                cc_id = generate_ulid()
                db.add(ChannelConfig(
                    id=cc_id,
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    channel_type=ch_type,
                    provider=provider,
                    name=f"{role}: {ch_type}" if role != "channel" else ch_type,
                    config={
                        "role": role,
                        "purpose": purpose,
                        "linked_service_key": linked_service_key,
                    },
                ))

                # Also create the Channel binding row so the gateway
                # can route inbound messages to the right agent.
                matched_sub = sub_by_service.get(linked_service_key) if linked_service_key else None
                # Fallback: first subscription
                if not matched_sub and subs:
                    matched_sub = subs[0]

                db.add(Channel(
                    id=generate_ulid(),
                    entity_id=user.entity_id,
                    workspace_id=workspace_id,
                    type=ch_type,
                    name=ch_type,
                    agent_id=matched_sub.agent_id if matched_sub else None,
                    agent_subscription_id=matched_sub.id if matched_sub else None,
                    config={"channel_config_id": cc_id},
                    status="active",
                ))

            resolved.append(provider)
        else:
            remaining.append(flag)

    # Update workspace settings — keep only unresolved flags
    if resolved:
        settings["flagged_integrations"] = remaining
        ws.settings = settings
        await db.flush()

    return {
        "resolved": resolved,
        "remaining": [f.get("provider", "") for f in remaining],
    }


# ── Documents scoped to workspace ───────────────────────────────────────

_WORKSPACE_GROUP_DEFAULT_KIND = "workspace_collection"
_WORKSPACE_GROUP_FOLDER_KIND = "knowledge_net"
_WORKSPACE_GROUP_FILE_BUCKET_KIND = "workspace_files"
_WORKSPACE_DEFAULT_COLLECTION_NAME = "Workspace Knowledge"


def _workspace_group_settings(group: DocumentGroup) -> dict:
    return dict(group.settings or {})


def _workspace_group_kind(group: DocumentGroup) -> str:
    settings = _workspace_group_settings(group)
    if settings.get("workspace_file_bucket"):
        return _WORKSPACE_GROUP_FILE_BUCKET_KIND
    if settings.get("default_collection"):
        return _WORKSPACE_GROUP_DEFAULT_KIND
    kind = str(settings.get("kind") or _WORKSPACE_GROUP_FOLDER_KIND)
    return _WORKSPACE_GROUP_FOLDER_KIND if kind == "knowledge_folder" else kind


def _workspace_group_network_type(group: DocumentGroup) -> str:
    return "workspace" if group.workspace_id else "global"


def _is_workspace_default_collection(group: DocumentGroup) -> bool:
    settings = _workspace_group_settings(group)
    return bool(settings.get("default_collection")) or _workspace_group_kind(group) == _WORKSPACE_GROUP_DEFAULT_KIND


def _workspace_group_purpose(group: DocumentGroup) -> str:
    settings = _workspace_group_settings(group)
    return str(settings.get("purpose") or "")


async def _ensure_default_workspace_collection(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> DocumentGroup:
    """Ensure the workspace has a default big RAG collection.

    Optional Knowledge Nets are smaller document networks. The default collection is the place
    documents go when the user says "add this to the workspace" without choosing
    a net.
    """
    existing = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        )
    )).scalars().all()
    for group in existing:
        if (group.settings or {}).get("workspace_file_bucket"):
            continue
        if _is_workspace_default_collection(group):
            return group

    from packages.core.models.base import generate_ulid

    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=_WORKSPACE_DEFAULT_COLLECTION_NAME,
        settings={
            "kind": _WORKSPACE_GROUP_DEFAULT_KIND,
            "default_collection": True,
            "purpose": "General workspace knowledge available to agents.",
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.flush()
    return group


async def _require_workspace_document_group(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    group_id: str,
) -> DocumentGroup:
    group = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.id == group_id,
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(404, "Workspace knowledge collection not found")
    return group


def _remove_group_from_operating_model(ws, group_id: str) -> None:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    knowledge["default_group_ids"] = [
        gid for gid in list(knowledge.get("default_group_ids") or [])
        if gid != group_id
    ]
    purposes = dict(knowledge.get("group_purposes") or {})
    purposes.pop(group_id, None)
    knowledge["group_purposes"] = purposes
    operating_model["knowledge"] = knowledge
    ws.operating_model = operating_model


def _ensure_group_default_in_operating_model(ws, group_id: str) -> bool:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    default_ids = list(knowledge.get("default_group_ids") or [])
    if group_id in default_ids:
        return False
    knowledge["default_group_ids"] = [group_id, *default_ids]
    operating_model["knowledge"] = knowledge
    ws.operating_model = operating_model
    return True


def _set_group_purpose_in_operating_model(ws, group_id: str, purpose: str) -> None:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    purposes = dict(knowledge.get("group_purposes") or {})
    if purpose:
        purposes[group_id] = purpose
    else:
        purposes.pop(group_id, None)
    knowledge["group_purposes"] = purposes
    operating_model["knowledge"] = knowledge
    ws.operating_model = operating_model


@router.get("/{workspace_id}/documents")
async def list_workspace_documents(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List document groups for a workspace, with document counts and member details."""
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroupMember, Document
    from packages.core.services.document_access import user_can_read_document

    ws = await _require_workspace_read(db, workspace_id, user)
    default_group = await _ensure_default_workspace_collection(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    changed_default_ids = _ensure_group_default_in_operating_model(ws, default_group.id)
    await db.commit()
    if changed_default_ids:
        await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    result = await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == user.entity_id,
            DocumentGroup.workspace_id == workspace_id,
        ).order_by(DocumentGroup.created_at.asc())
    )
    groups = [
        group for group in result.scalars().all()
        if not (group.settings or {}).get("workspace_file_bucket")
    ]

    out = []
    grouped_doc_ids: set[str] = set()
    for dg in groups:
        # Get document count and document details for this group
        members_result = await db.execute(
            select(Document)
            .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
            .where(
                DocumentGroupMember.group_id == dg.id,
                Document.entity_id == user.entity_id,
            )
        )
        docs = []
        for doc in members_result.scalars().all():
            if not await user_can_read_document(
                db,
                doc,
                entity_id=user.entity_id,
                user_id=user.id,
                role=user.role,
                workspace_id=workspace_id,
            ):
                continue
            docs.append({
                "id": doc.id,
                "name": doc.name,
                "file_type": doc.file_type,
                "file_size": doc.file_size,
                "vector_status": doc.vector_status,
            })
        grouped_doc_ids.update(d["id"] for d in docs)
        out.append({
            "id": dg.id,
            "entity_id": dg.entity_id,
            "workspace_id": dg.workspace_id,
            "name": dg.name,
            "kind": _workspace_group_kind(dg),
            "network_type": _workspace_group_network_type(dg),
            "scope": _workspace_group_network_type(dg),
            "is_knowledge_net": not bool((dg.settings or {}).get("workspace_file_bucket")),
            "purpose": _workspace_group_purpose(dg),
            "is_workspace_file_bucket": bool((dg.settings or {}).get("workspace_file_bucket")),
            "is_default_collection": _is_workspace_default_collection(dg),
            "vector_store_id": dg.vector_store_id,
            "settings": dg.settings or {},
            "created_at": dg.created_at.isoformat() if dg.created_at else None,
            "document_count": len(docs),
            "documents": docs,
        })
    artifact_result = await db.execute(
        select(Document)
        .where(
            Document.entity_id == user.entity_id,
            Document.is_trashed == False,  # noqa: E712
            Document.metadata_["origin"]["workspace_id"].astext == workspace_id,
        )
        .order_by(Document.created_at.desc())
        .limit(100)
    )
    artifact_docs = []
    for doc in artifact_result.scalars().all():
        if doc.id in grouped_doc_ids:
            continue
        if not await user_can_read_document(
            db,
            doc,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
            workspace_id=workspace_id,
        ):
            continue
        artifact_docs.append({
            "id": doc.id,
            "name": doc.name,
            "file_type": doc.file_type,
            "file_size": doc.file_size,
            "vector_status": doc.vector_status,
        })
    if artifact_docs:
        out.append({
            "id": f"{workspace_id}:generated_artifacts",
            "entity_id": user.entity_id,
            "workspace_id": workspace_id,
            "name": "Generated artifacts",
            "kind": "workspace_artifacts",
            "network_type": "artifacts",
            "scope": "artifacts",
            "is_knowledge_net": False,
            "purpose": "Files generated by workspace tasks and agents.",
            "is_workspace_file_bucket": True,
            "is_default_collection": False,
            "vector_store_id": None,
            "settings": {"generated_artifacts": True, "readonly": True},
            "created_at": None,
            "document_count": len(artifact_docs),
            "documents": artifact_docs,
        })
    return out


@router.post("/{workspace_id}/documents/groups", status_code=201)
async def create_workspace_document_group(
    workspace_id: str,
    req: WorkspaceKnowledgeGroupCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a user-manageable Knowledge Net scoped to this workspace."""
    from packages.core.models.base import generate_ulid

    ws = await _require_workspace_manage(db, workspace_id, user)
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Knowledge Net name is required")
    kind = (req.kind or _WORKSPACE_GROUP_FOLDER_KIND).strip() or _WORKSPACE_GROUP_FOLDER_KIND
    if kind == "knowledge_folder":
        kind = _WORKSPACE_GROUP_FOLDER_KIND
    if kind == _WORKSPACE_GROUP_DEFAULT_KIND:
        existing = await _ensure_default_workspace_collection(
            db,
            workspace_id=workspace_id,
            entity_id=user.entity_id,
        )
        _ensure_group_default_in_operating_model(ws, existing.id)
        await db.commit()
        await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)
        return {
            "id": existing.id,
            "entity_id": existing.entity_id,
            "workspace_id": existing.workspace_id,
            "name": existing.name,
            "kind": _workspace_group_kind(existing),
            "network_type": _workspace_group_network_type(existing),
            "scope": _workspace_group_network_type(existing),
            "is_knowledge_net": True,
            "purpose": _workspace_group_purpose(existing),
            "is_workspace_file_bucket": False,
            "is_default_collection": True,
            "settings": existing.settings or {},
            "document_count": 0,
            "documents": [],
        }
    group = DocumentGroup(
        id=generate_ulid(),
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        name=name,
        settings={
            "kind": kind,
            "scope": "workspace",
            "purpose": (req.purpose or "").strip(),
            "user_manageable": True,
        },
    )
    db.add(group)
    await db.commit()
    await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    await db.refresh(group)
    return {
        "id": group.id,
        "entity_id": group.entity_id,
        "workspace_id": group.workspace_id,
        "name": group.name,
        "kind": _workspace_group_kind(group),
        "network_type": _workspace_group_network_type(group),
        "scope": _workspace_group_network_type(group),
        "is_knowledge_net": True,
        "purpose": _workspace_group_purpose(group),
        "is_workspace_file_bucket": False,
        "is_default_collection": _is_workspace_default_collection(group),
        "settings": group.settings or {},
        "document_count": 0,
        "documents": [],
    }


@router.put("/{workspace_id}/documents/groups/{group_id}")
async def update_workspace_document_group(
    workspace_id: str,
    group_id: str,
    req: WorkspaceKnowledgeGroupUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a workspace knowledge collection's display metadata."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    group = await _require_workspace_document_group(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        group_id=group_id,
    )
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "Knowledge Net name cannot be empty")
        group.name = name
    settings = _workspace_group_settings(group)
    if req.purpose is not None:
        purpose = req.purpose.strip()
        settings["purpose"] = purpose
        _set_group_purpose_in_operating_model(ws, group.id, purpose)
    if req.kind is not None and not settings.get("workspace_file_bucket") and not _is_workspace_default_collection(group):
        next_kind = req.kind.strip() or _WORKSPACE_GROUP_FOLDER_KIND
        settings["kind"] = _WORKSPACE_GROUP_FOLDER_KIND if next_kind == "knowledge_folder" else next_kind
    group.settings = settings
    await db.commit()
    await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    await db.refresh(group)
    return {
        "id": group.id,
        "entity_id": group.entity_id,
        "workspace_id": group.workspace_id,
        "name": group.name,
        "kind": _workspace_group_kind(group),
        "network_type": _workspace_group_network_type(group),
        "scope": _workspace_group_network_type(group),
        "is_knowledge_net": not bool((group.settings or {}).get("workspace_file_bucket")),
        "purpose": _workspace_group_purpose(group),
        "is_workspace_file_bucket": bool((group.settings or {}).get("workspace_file_bucket")),
        "is_default_collection": _is_workspace_default_collection(group),
        "settings": group.settings or {},
    }


@router.delete("/{workspace_id}/documents/groups/{group_id}", status_code=204)
async def delete_workspace_document_group(
    workspace_id: str,
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a workspace Knowledge Net without deleting its documents."""
    from packages.core.models.document import DocumentGroupMember

    ws = await _require_workspace_manage(db, workspace_id, user)
    group = await _require_workspace_document_group(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        group_id=group_id,
    )
    if (group.settings or {}).get("workspace_file_bucket"):
        raise HTTPException(400, "Workspace Files group cannot be deleted; remove individual documents instead")
    if _is_workspace_default_collection(group):
        raise HTTPException(400, "Default workspace knowledge collection cannot be deleted; remove individual documents instead")
    await db.execute(
        sa_delete(DocumentGroupMember).where(DocumentGroupMember.group_id == group_id)
    )
    await db.delete(group)
    _remove_group_from_operating_model(ws, group_id)
    await db.commit()
    await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)


@router.post("/{workspace_id}/documents/groups/{group_id}/members")
async def add_workspace_document_group_members(
    workspace_id: str,
    group_id: str,
    req: WorkspaceKnowledgeMembersRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach existing Knowledge documents to a workspace group."""
    from packages.core.services.document_service import add_document_to_group
    from packages.core.services.document_access import get_visible_document

    await _require_workspace_manage(db, workspace_id, user)
    await _require_workspace_document_group(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        group_id=group_id,
    )
    doc_ids = []
    seen = set()
    for doc_id in req.document_ids or []:
        doc_id = str(doc_id or "").strip()
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            doc_ids.append(doc_id)
    if not doc_ids:
        raise HTTPException(400, "document_ids is required")

    added = 0
    skipped: list[str] = []
    for doc_id in doc_ids:
        doc = await get_visible_document(
            db,
            doc_id,
            user.entity_id,
            user_id=user.id,
            role=user.role,
            workspace_id=workspace_id,
        )
        if not doc:
            skipped.append(doc_id)
            continue
        if await add_document_to_group(db, doc.id, group_id, entity_id=user.entity_id):
            added += 1
        else:
            skipped.append(doc_id)
    await db.commit()
    if added:
        await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)
    return {"added": added, "skipped": skipped, "total": len(doc_ids)}


@router.delete("/{workspace_id}/documents/groups/{group_id}/members/{document_id}", status_code=204)
async def remove_workspace_document_group_member(
    workspace_id: str,
    group_id: str,
    document_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Detach a document from a workspace group without deleting the document."""
    from packages.core.models.document import Document, DocumentGroupMember

    await _require_workspace_manage(db, workspace_id, user)
    await _require_workspace_document_group(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        group_id=group_id,
    )
    doc_exists = (await db.execute(
        select(Document.id).where(
            Document.id == document_id,
            Document.entity_id == user.entity_id,
        ).limit(1)
    )).scalar_one_or_none()
    if not doc_exists:
        raise HTTPException(404, "Document not found")
    result = await db.execute(
        sa_delete(DocumentGroupMember).where(
            DocumentGroupMember.group_id == group_id,
            DocumentGroupMember.document_id == document_id,
        )
    )
    await db.commit()
    if result.rowcount:
        await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)


# ── Sandbox demo ──────────────────────────────────────────────────────

class SandboxCreateRequest(BaseModel):
    name: str | None = None
    kind: str = "social_media"
    seed_task_title: str | None = None


class SandboxCreateResponse(BaseModel):
    workspace_id: str
    agent_id: str
    subscription_id: str
    goal_id: str
    task_id: str
    chat_url: str


@router.post("/sandbox", response_model=SandboxCreateResponse, status_code=201)
async def create_sandbox(
    req: SandboxCreateRequest | None = None,
    _gate=Depends(require_plan("workspaces")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One-click "Try a demo" — provisions a complete sandbox workspace
    (workspace + agent + subscription + goal + 1 starter task) so the
    user can see the full Strategist→Planner→Executor→chat pipeline
    without connecting any real integrations.

    Returns the ids the UI needs to navigate to the chat view.
    """
    from packages.core.workspaces import (
        create_sandbox_workspace,
        sandbox_demo_name,
        sandbox_demo_services,
    )
    from packages.core.memory.canonical import ensure_workspace_memory_docs
    from packages.core.memory.repo import ensure_workspace_memory_dirs
    from packages.core.memory.seed import seed_workspace_memory
    from packages.core.services.entity_fs import (
        is_fs_enabled,
        provision_entity_filesystem,
    )

    req = req or SandboxCreateRequest()
    seed_kwargs: dict = {"entity_id": user.entity_id, "kind": req.kind}
    if req.name:
        seed_kwargs["name"] = req.name
    if req.seed_task_title:
        seed_kwargs["seed_task_title"] = req.seed_task_title

    ids = await create_sandbox_workspace(db, **seed_kwargs)
    ws = await _require_workspace(db, ids["workspace_id"], user.entity_id)
    settings = settings_with_default_workspace_access(ws.settings)
    settings.setdefault("created_by_user_id", user.id)
    ws.settings = settings
    await ensure_workspace_owner_membership(
        db,
        entity_id=user.entity_id,
        workspace_id=ws.id,
        user_id=user.id,
        added_by=user.id,
    )
    from packages.core.services.plan_gate import invalidate_gate_cache

    invalidate_gate_cache(user.entity_id)
    await db.commit()

    # Memory layout — same hook the regular setup wizard uses, isolated
    # so the workspace tx commits even if the FS / memory write fails.
    memory_workspace_name = req.name or sandbox_demo_name(req.kind)
    memory_services = sandbox_demo_services(req.kind)
    if is_fs_enabled():
        try:
            provision_entity_filesystem(user.entity_id)
            ensure_workspace_memory_dirs(user.entity_id, ids["workspace_id"])
            ensure_workspace_memory_docs(
                user.entity_id,
                ids["workspace_id"],
                workspace_name=memory_workspace_name,
                workspace_kind=req.kind,
            )
            await seed_workspace_memory(
                db,
                entity_id=user.entity_id,
                workspace_id=ids["workspace_id"],
                workspace_name=memory_workspace_name,
                workspace_kind=req.kind,
                services=memory_services,
            )
            await db.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "sandbox: memory seeding failed (workspace still usable)",
                exc_info=True,
            )

    return SandboxCreateResponse(
        chat_url=f"/api/v1/workspaces/{ids['workspace_id']}/chat/messages",
        **ids,
    )


# ── Evaluation ───────────────────────────────────────────────────────

@router.get("/{workspace_id}/evaluation")
async def get_workspace_evaluation(
    workspace_id: str,
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspace operating scorecard.

    Read-only snapshot across goals, costs, execution health, feedback,
    governance, and runtime learning. Strategist uses the same service so the
    UI and planning loop evaluate the workspace from identical evidence.
    """
    await _require_workspace_read(db, workspace_id, user)
    from packages.core.services.workspace_evaluation import build_workspace_evaluation

    return await build_workspace_evaluation(
        db,
        workspace_id,
        entity_id=user.entity_id,
        window_days=days,
    )


# ── Budget (M8) ──────────────────────────────────────────────────────

class BudgetStatusResponse(BaseModel):
    """Budget snapshot. ``*_credits`` is the user-facing surface; ``*_usd``
    is included for billing audit (precise storage rep)."""

    # User-facing (credits — what the UI shows)
    monthly_budget_credits: int | None
    monthly_spent_credits: int
    monthly_remaining_credits: int | None
    pct_used: float | None
    """0..>1.0 — None when no budget cap is set."""

    # State + behaviour
    alert_state: str | None
    auto_pause_on_budget: bool
    budget_reset_at: datetime | None
    days_until_month_end: int

    # Audit / admin (USD — precise storage)
    monthly_budget_usd: float | None
    monthly_spent_usd: float
    credits_per_usd: int


class BudgetUpdateRequest(BaseModel):
    monthly_budget_credits: int | None = None
    """Preferred — matches the UI. Pass 0 to clear the cap."""
    monthly_budget_usd: float | None = None
    """Admin / billing path. Ignored when ``monthly_budget_credits`` set."""
    auto_pause_on_budget: bool | None = None
    reset_alert_state: bool = True


@router.get("/{workspace_id}/budget", response_model=BudgetStatusResponse)
async def get_workspace_budget(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-month budget snapshot for the workspace."""
    await _require_workspace_read(db, workspace_id, user)
    from packages.core.budget import get_budget_status
    status = await get_budget_status(db, workspace_id)
    if status is None:
        raise HTTPException(404, "workspace not found")
    return BudgetStatusResponse(**status.__dict__)


@router.put("/{workspace_id}/budget", response_model=BudgetStatusResponse)
async def update_workspace_budget(
    workspace_id: str,
    req: BudgetUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Raise / lower / clear the cap. Caller commits via FastAPI."""
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.budget import get_budget_status

    budget_payload: dict[str, Any] = {"reset_alert_state": req.reset_alert_state}
    if "monthly_budget_credits" in req.model_fields_set:
        budget_payload["monthly_budget_credits"] = req.monthly_budget_credits
    if "monthly_budget_usd" in req.model_fields_set:
        budget_payload["monthly_budget_usd"] = req.monthly_budget_usd
    if "auto_pause_on_budget" in req.model_fields_set:
        budget_payload["auto_pause_on_budget"] = req.auto_pause_on_budget

    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_budget_update",
        patches=[{
            "op": "budget_policy.update",
            "payload": budget_payload,
        }],
    )
    status = await get_budget_status(db, workspace_id)
    await db.commit()
    return BudgetStatusResponse(**status.__dict__)
