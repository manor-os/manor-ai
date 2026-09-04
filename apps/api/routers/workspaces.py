"""Workspace endpoints — CRUD, operating model, agent mappings, activity, setup."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, select, delete as sa_delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import is_master_agent
from packages.core.contracts.webchat_page import ResolvedWorkspaceContent, WebchatPage
from packages.core.constants.document_groups import WorkspaceDocumentGroupKind
from packages.core.constants.goals import GoalStatus
from packages.core.constants.notification_types import NotificationOutboxStatus
from packages.core.goals.numbers import GoalNumberInput
from packages.core.constants.task import TaskStatus
from packages.core.constants.execution import (
    ExecutionStepStatus,
)
from packages.core.database import get_db
from packages.core.models.user import Entity, User
from packages.core.models.event import EventLog
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import DocumentGroup
from packages.core.permissions import user_is_effective_entity_admin
from packages.core.services.entity_service import (
    ProtectedWorkspaceSettingsError,
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
    lock_workspace_access_boundary,
    manageable_workspace_ids_for_user,
    settings_with_default_workspace_access,
    user_can_manage_workspace,
)
from packages.core.services.actor_authorization import AuthenticatedUserCredential
from packages.core.services.permission_gate import ResourcePermissionGate
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
    provider_keys_match,
)
from packages.core.services.oauth_account_credentials import (
    oauth_account_is_runtime_usable_clause,
)
from packages.core.services.tool_cache_version import bump_tool_cache_version
from packages.core.services.document_service import (
    create_workspace_knowledge_group,
    mark_workspace_knowledge_changed,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_references,
)
from apps.api.deps import get_current_user, require_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


# ── Request / Response models ────────────────────────────────────────────────

_SUPPORTED_CHANNEL_LANGUAGES = {"en", "zh", "es", "de"}
_WEBCHAT_RESOURCE_LIMIT = 200
_WEBCHAT_DOCUMENT_SCAN_BATCH = 200
_WEBCHAT_DOCUMENT_SCAN_LIMIT = 1000
_WEBCHAT_DOCUMENT_CURSOR_LIMIT = 1_000_000


def _normalize_channel_language(value: Any) -> str:
    base = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return base if base in _SUPPORTED_CHANNEL_LANGUAGES else "en"


def _normalized_channel_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    raw_language = cfg.get("language") or cfg.pop("locale", None)
    cfg["language"] = _normalize_channel_language(raw_language)
    return cfg


async def _resolved_webchat_page_config(
    db: AsyncSession,
    config: dict[str, Any],
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Normalize public references so stored content matches server Review."""
    if config.get("public_page") is None:
        return config
    from packages.core.services.webchat_page import (
        resolve_workspace_webchat_page,
        webchat_page_for_storage,
    )

    resolved = await resolve_workspace_webchat_page(
        db,
        value=config["public_page"],
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if resolved is None:
        raise HTTPException(422, "Invalid Webchat page")
    return {
        **config,
        "public_page": webchat_page_for_storage(resolved).model_dump(exclude_none=True),
    }


class WorkspaceResponse(BaseModel):
    id: str
    entity_id: str
    name: str
    artifact_folder_id: str | None = None
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
    can_manage: bool = False
    #: Whether the blueprint this workspace was installed from has changed
    #: since. Detection only — applying an update is a deliberate act.
    blueprint_update: dict | None = None
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


class WorkspaceSetupStatusResponse(BaseModel):
    ready: bool
    status: str
    summary: str
    incomplete_checks: list[dict[str, Any]] = Field(default_factory=list)


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


class WorkspaceLedgerContractsRequest(BaseModel):
    ledger_contracts: list[Any] = Field(default_factory=list)


class WorkspaceResumeGoalRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    target_value: GoalNumberInput = 1


class WorkspaceResumeRequest(BaseModel):
    goal: WorkspaceResumeGoalRequest | None = None


class StaffAssignRequest(BaseModel):
    staff_id: str
    role: str | None = None
    # User-linked memberships require a workspace role (owner / editor /
    # contributor / viewer). Unlinked staff assignments may keep a legacy
    # business label for backward compatibility.
    expires_at: datetime | None = None
    user_id: str | None = None


class StaffResponse(BaseModel):
    id: str
    workspace_id: str
    staff_id: str | None = None
    user_id: str | None = None
    display_name: str | None = None
    email: str | None = None
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


class WorkspaceKnowledgeFolderAddResponse(BaseModel):
    group_id: str
    group_name: str
    created: bool
    added: int
    existing: int
    total: int


class ServiceRequest(BaseModel):
    key: str
    name: str
    description: str = ""
    config: dict = Field(default_factory=dict)


class AgentMappingRequest(BaseModel):
    service_key: str
    agent_id: str
    custom_prompt: str | None = None


class WorkspaceChannelConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def validate_public_page(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("public_page") is not None:
            page = WebchatPage.model_validate(value["public_page"])
            # Resolved Workspace content is a read-time projection. Never
            # trust or persist the browser's preview snapshot.
            for module in page.modules:
                if getattr(module, "type", None) == "workspace_content":
                    module.resolved = None
            value = {
                **value,
                "public_page": page.model_dump(exclude_none=True),
            }
        return value


class WorkspaceChannelRequest(WorkspaceChannelConfigRequest):
    channel_config_id: str | None = None
    channel_type: str = "webchat"
    name: str | None = None
    purpose: str | None = None
    role: str = "primary_external"
    linked_service_key: str | None = None
    agent_subscription_id: str | None = None
    agent_id: str | None = None


class WorkspaceChannelUpdateRequest(WorkspaceChannelConfigRequest):
    name: str | None = None
    purpose: str | None = None
    role: str | None = None
    linked_service_key: str | None = None
    agent_subscription_id: str | None = None
    agent_id: str | None = None


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


def _workspace_blueprint_id(settings: Any) -> str:
    """Return the durable blueprint id, including the legacy built-in repair.

    Older built-in installs wrote ``blueprint_slug`` but accidentally left
    ``blueprint_id`` null. Platform blueprints are ordinary marketplace rows
    now, with the stable id ``builtin:<slug>``. Resolve that historical shape
    to the same row so existing workspaces can see and apply updates instead
    of remaining permanently detached.
    """
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        installed_blueprint_record,
    )
    from packages.core.blueprints.seed import platform_blueprint_id

    record = installed_blueprint_record(settings)
    blueprint_id = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    if blueprint_id:
        return blueprint_id
    slug = str(record.get("blueprint_slug") or "").strip()
    return platform_blueprint_id(slug) if slug else ""


async def _blueprint_payloads_for(db: AsyncSession, workspaces) -> dict[str, tuple]:
    """Map each workspace to the blueprint payload it would upgrade toward.

    Every current blueprint lives in ``workspace_blueprints``. New installs
    record its id; the helper above maps the historical built-in slug-only
    shape to that same stable row id.

    One query for all of them: this feeds the workspace list.
    """
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        installed_blueprint_record,
    )
    from packages.core.blueprints.seed import (
        BlueprintRowReference,
        resolve_blueprint_rows,
    )

    payloads: dict[str, tuple] = {}
    references: dict[str, BlueprintRowReference] = {}

    for ws in workspaces:
        settings = ws.settings if isinstance(getattr(ws, "settings", None), dict) else {}
        record = installed_blueprint_record(settings)
        blueprint_id = str(record.get(BLUEPRINT_ID_KEY) or "").strip() or None
        blueprint_slug = str(record.get("blueprint_slug") or "").strip() or None
        if blueprint_id or blueprint_slug:
            references[ws.id] = BlueprintRowReference(
                blueprint_id,
                blueprint_slug,
            )

    if not references:
        return payloads

    try:
        rows = await resolve_blueprint_rows(db, references)
        from packages.core.constants.blueprints import BlueprintStatus

        for workspace_id, row in rows.items():
            payload = row.payload if isinstance(row.payload, dict) else None
            # Only a published row is an update release. Archived/draft
            # content is editable and must never leak through upgrade preview
            # or be applied to an installed Workspace.
            if payload and row.status == BlueprintStatus.PUBLISHED:
                payloads[workspace_id] = (payload, row.content_version, row.id)
    except Exception:
        logger.warning("blueprint freshness: payload lookup failed", exc_info=True)

    return payloads


def _blueprint_update_for(ws, resolved: tuple | None = None) -> dict[str, Any] | None:
    """Whether this workspace's blueprint has moved on without it.

    Best-effort: a workspace page must render whether or not the blueprint it
    names can still be read.
    """
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        BlueprintFreshness,
        blueprint_update_summary,
        installed_blueprint_record,
    )

    settings = ws.settings if isinstance(getattr(ws, "settings", None), dict) else {}
    if not _workspace_blueprint_id(settings):
        return None

    payload, current_version, resolved_id = resolved if resolved else (None, None, None)
    effective_settings = settings
    record = installed_blueprint_record(settings)
    if resolved_id and record.get(BLUEPRINT_ID_KEY) != resolved_id:
        # Read-time compatibility for installs created by the old built-in
        # route. The POST below persists this repair when the operator elects
        # to update; a GET remains read-only.
        effective_settings = dict(settings)
        effective_record = dict(record)
        effective_record[BLUEPRINT_ID_KEY] = resolved_id
        effective_settings["_blueprint"] = effective_record
    summary = blueprint_update_summary(
        effective_settings, payload, current_version=current_version,
    )
    if summary.get("status") == BlueprintFreshness.NOT_FROM_BLUEPRINT.value:
        return None
    return summary


def _to_response(
    ws,
    *,
    creator: User | dict[str, Any] | None = None,
    blueprint_payload: tuple | None = None,
    can_manage: bool = False,
) -> WorkspaceResponse:
    creator_summary = _coerce_user_summary(creator)
    return WorkspaceResponse(
        id=ws.id, entity_id=ws.entity_id, name=ws.name,
        artifact_folder_id=getattr(ws, "artifact_folder_id", None),
        description=ws.description, category=ws.category,
        address=ws.address, kind=ws.kind,
        operating_context=ws.operating_context,
        primary_work=ws.primary_work,
        operating_model=ws.operating_model or {},
        settings=ws.settings or {}, status=ws.status,
        blueprint_update=_blueprint_update_for(ws, blueprint_payload),
        created_at=ws.created_at, updated_at=getattr(ws, "updated_at", None),
        created_by_user_id=(creator_summary or {}).get("id"),
        created_by_name=(creator_summary or {}).get("name"),
        created_by_email=(creator_summary or {}).get("email"),
        created_by_avatar_url=(creator_summary or {}).get("avatar_url"),
        can_manage=can_manage,
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


async def _require_workspace_read(
    db: AsyncSession,
    workspace_id: str,
    user: User,
    *,
    credential: AuthenticatedUserCredential | None = None,
):
    ws = await _require_workspace(db, workspace_id, user.entity_id)
    authorized = await ResourcePermissionGate.authorize_workspace_read(
        db,
        credential=credential or AuthenticatedUserCredential.from_user(user),
        workspace_id=workspace_id,
    )
    if authorized is None:
        raise HTTPException(404, "Workspace not found")
    return ws


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
    # Management endpoints frequently read/merge/write the Workspace JSON
    # settings document. Lock before both authorization and the read so two
    # independent patches cannot restore a stale access_mode value.
    ws = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    if not ws or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    if await user_can_manage_workspace(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        entity_role=user.role,
    ):
        return ws
    if await ResourcePermissionGate.authorize_workspace_read(
        db,
        credential=AuthenticatedUserCredential.from_user(user),
        workspace_id=workspace_id,
    ) is None:
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
    await mark_workspace_knowledge_changed(entity_id, workspace_id)


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

    credential = AuthenticatedUserCredential.from_user(user)
    workspaces = await list_workspaces(db, user.entity_id)
    authorized = await ResourcePermissionGate.authorize_workspace_batch_read(
        db,
        credential=credential,
        workspace_ids={str(workspace.id) for workspace in workspaces},
    )
    if authorized is None:
        raise HTTPException(404, "Workspace not found")
    workspaces = [
        workspace
        for workspace in workspaces
        if str(workspace.id) in authorized.workspace_ids
    ]

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
            "open_approval_requests": 0,
        }
        for wid in ws_ids
    }

    if ws_ids:
        # Unified approval store: open requests per workspace — the truth the
        # cards/badge derive from, exposed directly so the UI can render an
        # approvals count that stale or duplicate cards cannot inflate.
        # SAVEPOINT so a missing hitl_requests table (mid-rollout, replica
        # behind on migrations) degrades to count=0 WITHOUT aborting the outer
        # transaction — a bare try/except would poison every later query in
        # this request and 500 the whole sidebar endpoint.
        # NOTE: the response key stays ``open_approval_requests`` — it is a
        # published API field name, and renaming it would break clients.
        try:
            from packages.core.governance.approvals import (
                count_open_requests_by_workspace,
            )

            async with db.begin_nested():
                counts = await count_open_requests_by_workspace(
                    db, workspace_ids=ws_ids,
                )
            for wid, cnt in counts.items():
                if wid in stats_map:
                    stats_map[wid]["open_approval_requests"] = cnt
        except Exception:
            pass  # hitl_requests table may not exist yet mid-rollout

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
                Goal.status == GoalStatus.ACTIVE.value,
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
            # SAVEPOINT for the same reason as the approval-requests block
            # above: a bare try/except around a failing query leaves the outer
            # transaction aborted, so every later query in this request fails
            # too and the whole sidebar 500s instead of losing one stat.
            async with db.begin_nested():
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
                # `approve_proposals` and `retry_strategist_review` get their
                # own buckets so the UI can style them differently. EVERY other
                # kind counts as a plain chat action — including ones added
                # after this code was written. An `elif` chain that silently
                # dropped unknown kinds made `external_message_approval`
                # (approve a post/tweet) and `needs_login` invisible to the
                # badge while the chat rendered them as actionable cards.
                if action_kind == "approve_proposals":
                    stats_map[r.workspace_id]["proposal_actions"] += 1
                elif action_kind == "retry_strategist_review":
                    stats_map[r.workspace_id]["failed_actions"] += 1
                else:
                    stats_map[r.workspace_id]["chat_pending_actions"] += 1
                    if action.get("step_id"):
                        chat_hitl_step_ids.setdefault(r.workspace_id, set()).add(action["step_id"])
                    if action.get("task_id"):
                        chat_hitl_task_ids.setdefault(r.workspace_id, set()).add(action["task_id"])
        except Exception:
            # Degrading to 0 hides every badge in the sidebar, so say why.
            logger.warning("workspace chat pending-action stats failed", exc_info=True)

        hitl_task_ids: dict[str, set[str]] = {}
        waiting_step_ids: dict[str, set[str]] = {}
        waiting_step_task_ids: dict[str, set[str]] = {}

        waiting_task_rows = (await db.execute(
            select(Task.workspace_id, Task.id)
            .where(
                Task.workspace_id.in_(ws_ids),
                Task.entity_id == user.entity_id,
                Task.status == TaskStatus.WAITING_ON_CUSTOMER,
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
                ExecutionStep.step_status == ExecutionStepStatus.WAITING_HUMAN,
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
    blueprint_payloads = await _blueprint_payloads_for(db, workspaces)
    final_authorized = await ResourcePermissionGate.authorize_workspace_batch_read(
        db,
        credential=credential,
        workspace_ids={str(workspace.id) for workspace in workspaces},
    )
    if final_authorized is None:
        raise HTTPException(404, "Workspace not found")
    workspaces = [
        workspace
        for workspace in workspaces
        if str(workspace.id) in final_authorized.workspace_ids
    ]
    manageable_workspace_ids = await manageable_workspace_ids_for_user(
        db,
        workspaces=workspaces,
        entity_id=final_authorized.actor.entity_id,
        user_id=final_authorized.actor.user_id,
        entity_role=final_authorized.actor.role,
    )
    result = []
    for ws in workspaces:
        resp = _to_response(
            ws,
            creator=creator_map.get(ws.id),
            blueprint_payload=blueprint_payloads.get(ws.id),
            can_manage=ws.id in manageable_workspace_ids,
        )
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
    return _to_response(ws, creator=user, can_manage=True)


# ── Trash / Restore ─────────────────────────────────────────────────────────

@router.get("/trash/list", response_model=list[WorkspaceResponse])
async def list_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspaces in the soft-delete grace window. Returns rows with
    ``deleted_at`` set; consumers can compute days-until-purge from
    that timestamp + ``WORKSPACE_PURGE_GRACE_DAYS``."""
    if not await user_is_effective_entity_admin(db, user):
        raise HTTPException(403, "Only owner/admin can view workspace trash")
    rows = await list_trashed_workspaces(db, user.entity_id)
    creator_map = await _workspace_creator_summaries(db, rows)
    payloads = await _blueprint_payloads_for(db, rows)
    return [
        _to_response(
            ws,
            creator=creator_map.get(ws.id),
            blueprint_payload=payloads.get(ws.id),
            can_manage=True,
        )
        for ws in rows
    ]


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
    credential = AuthenticatedUserCredential.from_user(user)
    ws = await _require_workspace_read(
        db, workspace_id, user, credential=credential,
    )
    creator_map = await _workspace_creator_summaries(db, [ws])
    blueprint_payload = (await _blueprint_payloads_for(db, [ws])).get(ws.id)
    final_authorized = await ResourcePermissionGate.authorize_workspace_read(
        db,
        credential=credential,
        workspace_id=workspace_id,
    )
    if final_authorized is None:
        raise HTTPException(404, "Workspace not found")
    can_manage = await user_can_manage_workspace(
        db,
        workspace_id=ws.id,
        user_id=final_authorized.actor.user_id,
        entity_role=final_authorized.actor.role,
    )
    return _to_response(
        ws,
        creator=creator_map.get(ws.id),
        blueprint_payload=blueprint_payload,
        can_manage=can_manage,
    )


@router.get(
    "/{workspace_id}/setup-status",
    response_model=WorkspaceSetupStatusResponse,
)
async def get_workspace_setup_status(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the live Blueprint setup gate, not its install-time snapshot."""

    workspace = await _require_workspace_read(db, workspace_id, user)
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
    )

    status = await evaluate_workspace_blocking_setup(db, workspace)
    if status is None:
        return WorkspaceSetupStatusResponse(
            ready=True,
            status="not_required",
            summary="No blocking Blueprint setup is required.",
        )
    return WorkspaceSetupStatusResponse(
        ready=not status.blocks_work,
        status=status.status,
        summary=status.summary,
        incomplete_checks=list(status.details.get("incomplete_checks") or []),
    )


@router.get("/{workspace_id}/ledgers/overview")
async def get_workspace_ledger_overview(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return aggregate-only Ledger projections for Workspace chat."""

    from packages.core.services.workspace_ledger_overview import (
        WorkspaceLedgerOverviewUnavailable,
        workspace_ledger_overview,
    )

    ws = await _require_workspace_read(db, workspace_id, user)
    try:
        return await workspace_ledger_overview(
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            settings=ws.settings,
        )
    except WorkspaceLedgerOverviewUnavailable as exc:
        raise HTTPException(
            503,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.put("/{workspace_id}/ledgers/configuration")
async def configure_workspace_ledgers(
    workspace_id: str,
    req: WorkspaceLedgerContractsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually replace installed Ledger contracts without touching other settings."""

    from packages.core.services.workspace_ledger_matching import (
        normalize_workspace_ledger_contracts,
    )
    from packages.core.services.ledger_query_service import (
        workspace_queryable_ledger_configs,
    )

    ws = await _require_workspace_manage(db, workspace_id, user)
    try:
        settings = ws.settings if isinstance(ws.settings, dict) else {}
        existing_source = (
            settings.get("ledger_contracts")
            if "ledger_contracts" in settings
            # Preserve legacy storage locations when the dialog first writes
            # the canonical contract list.
            else list(workspace_queryable_ledger_configs(settings).values())
        )
        existing = normalize_workspace_ledger_contracts(existing_source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    existing_by_contract = {item["contract_id"]: item for item in existing}
    contracts: list[dict[str, Any]] = []
    seen_contracts: set[str] = set()
    try:
        for raw in req.ledger_contracts:
            item = normalize_workspace_ledger_contracts([raw])[0]
            if item["contract_id"] in seen_contracts:
                continue
            contracts.append(
                existing_by_contract.get(item["contract_id"], item)
                if isinstance(raw, str)
                else item
            )
            seen_contracts.add(item["contract_id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    settings = dict(settings)
    settings["ledger_contracts"] = contracts
    settings.pop("ledger_matching", None)
    ws.settings = settings
    await db.flush()
    from packages.core.services.workspace_service import record_activity

    await record_activity(
        db,
        workspace_id,
        user.entity_id,
        event_type="workspace.ledgers.configured",
        summary="Workspace business Ledgers configured",
        details={"contract_ids": [item["contract_id"] for item in contracts]},
        user_id=user.id,
    )
    # The chat editor immediately refreshes the overview after this response.
    # Commit before returning so that read-after-write cannot observe the old
    # Ledger configuration while the request dependency is still unwinding.
    await db.commit()
    await bump_tool_cache_version(user.entity_id, "ledgers")
    return {
        "workspace_id": workspace_id,
        "ledger_contracts": contracts,
        "source": "manual",
    }


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
    update_fields = req.model_dump(exclude_unset=True)
    heartbeat_payload: dict[str, Any] = {}
    if "heartbeat_enabled" in req.model_fields_set:
        heartbeat_payload["enabled"] = bool(req.heartbeat_enabled)
        update_fields.pop("heartbeat_enabled", None)
    if "heartbeat_cadence" in req.model_fields_set:
        heartbeat_payload["cadence"] = req.heartbeat_cadence
        update_fields.pop("heartbeat_cadence", None)

    audit_fields = sorted(set(update_fields.keys()) | set(heartbeat_payload.keys()))
    clear_fields = {key for key, value in update_fields.items() if value is None}
    try:
        ws = await update_workspace(
            db,
            workspace_id,
            user.entity_id,
            clear_fields=clear_fields,
            **update_fields,
        )
    except ProtectedWorkspaceSettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    return _to_response(
        ws,
        creator=creator_map.get(ws.id),
        blueprint_payload=(await _blueprint_payloads_for(db, [ws])).get(ws.id),
        can_manage=True,
    )


@router.delete("/{workspace_id}", status_code=204)
async def delete_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a workspace and delete its associated automations.

    The workspace remains restorable for ``WORKSPACE_PURGE_GRACE_DAYS`` days;
    after that the nightly ``ops.purge_soft_deleted_workspaces`` task
    hard-deletes it. Restoring recreates built-in runtime jobs, not deleted
    user automations.
    """
    if not await user_is_effective_entity_admin(db, user):
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
    if not await user_is_effective_entity_admin(db, user):
        raise HTTPException(403, "Only owner/admin can restore workspaces")
    ws = await restore_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(
            404,
            "Workspace not found in trash (already purged or never deleted)",
        )
    embedding_document_ids = tuple(
        getattr(ws, "_restored_embedding_document_ids", ())
    )
    # The lifecycle row and cleared block markers must be visible before a
    # worker can claim any restored document.
    await db.commit()
    if embedding_document_ids:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        for document_id in embedding_document_ids:
            try:
                process_document_embeddings.delay(document_id)
            except Exception:
                logger.warning(
                    "Failed to dispatch restored document embedding %s",
                    document_id,
                    exc_info=True,
                )
    from packages.core.services.plan_gate import invalidate_gate_cache

    invalidate_gate_cache(user.entity_id)
    _invalidate_workspace_context(workspace_id)
    creator_map = await _workspace_creator_summaries(db, [ws])
    return _to_response(
        ws,
        creator=creator_map.get(ws.id),
        blueprint_payload=(await _blueprint_payloads_for(db, [ws])).get(ws.id),
        can_manage=True,
    )


# ── Pause / Resume ──────────────────────────────────────────────────────────

_WORKSPACE_LIFECYCLE_BODIES = {
    ("start", "completed"):
        "Workspace automation started · AI is tracking goals and preparing the next tasks.",
    ("start_without_goals", "completed"):
        "Workspace automation started without goals · AI is autonomously preparing the next tasks.",
    ("pause", "completed"):
        "Workspace automation paused · no new Strategist or Goal schedules will be created.",
}


async def _persist_workspace_lifecycle_activity(
    db: AsyncSession,
    workspace: Workspace,
    *,
    action: str,
    transition_id: str | None,
    use_goals: bool | None = None,
) -> str | None:
    """Commit a lifecycle receipt, then fan it out to open Workspace Chats."""
    from packages.core.workspace_chat import service as chat_service

    workspace_id = workspace.id
    entity_id = workspace.entity_id
    try:
        message = await chat_service.post_workspace_lifecycle_activity(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            body=_WORKSPACE_LIFECYCLE_BODIES[
                ("start_without_goals" if action == "start" and use_goals is False else action, "completed")
            ],
            action=action,
            phase="completed",
            transition_id=transition_id,
        )
        message_id = message.id
        await db.commit()
    except Exception:  # noqa: BLE001 — a receipt must not block lifecycle control
        await db.rollback()
        logger.debug(
            "Workspace lifecycle receipt skipped for %s action=%s",
            workspace_id,
            action,
            exc_info=True,
        )
        return None

    try:
        await chat_service.publish_workspace_chat_message_event(
            entity_id,
            workspace_id=workspace_id,
            message=message,
        )
    except Exception:  # noqa: BLE001 — realtime fanout is best-effort
        logger.debug(
            "Workspace lifecycle realtime publish skipped for %s action=%s",
            workspace_id,
            action,
            exc_info=True,
        )
    return message_id

@router.post("/{workspace_id}/pause")
async def pause_workspace(
    workspace_id: str,
    transition_id: str | None = Query(default=None, max_length=128),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pause a workspace — stops its automations and autonomous runtime."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    # Serialize pause with other Workspace lifecycle changes before canceling
    # pending external delivery for this Workspace.
    ws = (
        await db.execute(
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.entity_id == user.entity_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if ws is None:
        raise HTTPException(404, "Workspace not found")
    if ws.status == "paused":
        # Re-assert the lifecycle invariant in case an automation was created
        # or enabled after the workspace originally entered the paused state.
        from packages.core.services.scheduler_service import pause_workspace_automations

        await db.execute(
            update(EventLog)
            .where(
                EventLog.entity_id == user.entity_id,
                EventLog.workspace_id == workspace_id,
                EventLog.external_delivery_status.in_([
                    NotificationOutboxStatus.PENDING.value,
                    NotificationOutboxStatus.PROCESSING.value,
                ]),
            )
            .values(
                external_delivery_status=NotificationOutboxStatus.CANCELED.value,
                external_delivery_locked_until=None,
                external_delivery_claim_token=None,
                external_delivery_last_error=(
                    "workspace paused before external event delivery"
                ),
            )
        )
        await pause_workspace_automations(db, workspace_id, user.entity_id)
        await db.commit()
        return {"status": "paused", "workspace_id": workspace_id}
    if ws.status != "active":
        raise HTTPException(
            409,
            f"Workspace status is {ws.status!r}; only an active workspace can be paused",
        )
    ws.status = "paused"
    await db.execute(
        update(EventLog)
        .where(
            EventLog.entity_id == user.entity_id,
            EventLog.workspace_id == workspace_id,
            EventLog.external_delivery_status.in_([
                NotificationOutboxStatus.PENDING.value,
                NotificationOutboxStatus.PROCESSING.value,
            ]),
        )
        .values(
            external_delivery_status=NotificationOutboxStatus.CANCELED.value,
            external_delivery_locked_until=None,
            external_delivery_claim_token=None,
            external_delivery_last_error=(
                "workspace paused before external event delivery"
            ),
        )
    )
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_workspace_pause",
        patches=[{"op": "heartbeat_policy.update", "payload": {"enabled": False}}],
    )
    await db.commit()
    lifecycle_message_id = await _persist_workspace_lifecycle_activity(
        db,
        ws,
        action="pause",
        transition_id=transition_id,
    )
    return {
        "status": "paused",
        "workspace_id": workspace_id,
        "lifecycle_message_id": lifecycle_message_id,
    }


@router.post("/{workspace_id}/resume/v2")
@router.post("/{workspace_id}/resume")
async def resume_workspace(
    workspace_id: str,
    transition_id: str | None = Query(default=None, max_length=128),
    req: WorkspaceResumeRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start autonomous runtime, deriving Goal use from active Workspace Goals."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    from packages.core.workspaces import is_sandbox_workspace

    if is_sandbox_workspace(ws):
        raise HTTPException(
            409,
            "Workspace simulation cannot start the ordinary autonomous runtime",
        )
    from packages.core.goals.locking import lock_workspace_for_goal_mutation

    locked_ws = await lock_workspace_for_goal_mutation(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    if locked_ws is None:
        raise HTTPException(404, "Workspace not found")
    # The authorization lookup may have populated this identity before a
    # concurrent Goal mutation committed. Refresh only after owning the
    # canonical Workspace -> Goal lock so status and Goal mode are one snapshot.
    await db.refresh(locked_ws)
    ws = locked_ws
    if ws.status == "active" and ws.heartbeat_enabled and req is None:
        return {"status": "active", "workspace_id": workspace_id}
    if ws.status not in {"active", "paused"}:
        raise HTTPException(
            409,
            f"Workspace status is {ws.status!r}; only an active or paused workspace can start autonomous runtime",
        )

    from packages.core.models.goal import Goal

    goal = (await db.execute(
        select(Goal)
        .where(
            Goal.entity_id == user.entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == GoalStatus.ACTIVE.value,
        )
        .order_by(Goal.priority.desc(), Goal.created_at.desc(), Goal.id.asc())
        .limit(1)
    )).scalar_one_or_none()
    goal_created = False
    goal_updated = False
    created_goal_id: str | None = None
    if req is not None and req.goal is not None:
        from packages.core.goals import service as goal_service

        if goal is None:
            goal = await goal_service.create_goal(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                title=req.goal.title,
                metric_key=None,
                target_value=req.goal.target_value,
            )
            goal_created = True
            created_goal_id = goal.id
        elif goal.title != req.goal.title:
            updated_goal = await goal_service.update_goal(
                db,
                goal.id,
                user.entity_id,
                title=req.goal.title,
            )
            if updated_goal is None:
                raise HTTPException(404, "Goal not found")
            goal = updated_goal
            goal_updated = True

    use_goals = goal is not None
    ws.status = "active"
    patches: list[dict[str, Any]] = [
        {
            "op": "heartbeat_policy.update",
            "payload": {"enabled": True, "cadence": ws.heartbeat_cadence or "daily"},
        },
        {
            "op": "strategist.update",
            "payload": {"use_goals": use_goals},
        },
    ]
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_workspace_resume",
        patches=patches,
    )
    if goal_created:
        await sync_workspace_runtime_schedules(db, ws)
    await db.commit()
    lifecycle_message_id = await _persist_workspace_lifecycle_activity(
        db,
        ws,
        action="start",
        transition_id=transition_id,
        use_goals=use_goals,
    )
    return {
        "status": "active",
        "workspace_id": workspace_id,
        "heartbeat_enabled": True,
        "use_goals": use_goals,
        "goal_id": goal.id if goal is not None else None,
        "goal_created": goal_created,
        "goal_updated": goal_updated,
        "created_goal_id": created_goal_id,
        "lifecycle_message_id": lifecycle_message_id,
    }


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
    return await get_workspace_agent_mappings(
        db,
        workspace_id,
        user.entity_id,
        include_agent_identity=True,
    )


@router.get("/{workspace_id}/agents/assignable")
async def list_assignable_workspace_agents(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return only the active Agent IDs safe to assign to Workspace Tasks.

    Contributors may need this list to assign a task, but must not receive
    service mapping configuration or custom prompts reserved for managers.
    """
    from packages.core.models.workspace import AgentSubscription

    await _require_workspace_read(db, workspace_id, user)
    rows = await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )
    subscriptions = list(rows.scalars())
    return {
        "agent_ids": list(dict.fromkeys(
            subscription.agent_id
            for subscription in subscriptions
            if subscription.agent_id
        )),
        "subscriptions": [
            {
                "id": subscription.id,
                "agent_id": subscription.agent_id,
                "role_label": subscription.name or subscription.service_key,
            }
            for subscription in subscriptions
            if subscription.agent_id
        ],
    }


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


@router.get("/{workspace_id}/connection-status")
async def get_workspace_connection_status(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Current connection guidance; never mutates setup or account bindings."""
    from packages.core.services.workspace_connection_status import WorkspaceConnectionStatusFactory

    workspace = await _require_workspace_read(db, workspace_id, user)
    return await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user.id)


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
            oauth_account_is_runtime_usable_clause(),
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
    from packages.core.models.user import User as UserModel

    await _require_workspace_read(db, workspace_id, user)
    result = await db.execute(
        select(WorkspaceStaff, Staff, UserModel)
        .join(Workspace, Workspace.id == WorkspaceStaff.workspace_id)
        .outerjoin(
            Staff,
            and_(
                Staff.id == WorkspaceStaff.staff_id,
                Staff.entity_id == user.entity_id,
                Staff.deleted_at.is_(None),
            ),
        )
        .outerjoin(
            UserModel,
            and_(
                UserModel.id == WorkspaceStaff.user_id,
                UserModel.entity_id == user.entity_id,
                UserModel.deleted_at.is_(None),
            ),
        )
        .where(
            WorkspaceStaff.workspace_id == workspace_id,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
            WorkspaceStaff.status == "active",
            or_(
                and_(
                    WorkspaceStaff.staff_id.is_not(None),
                    Staff.id.is_not(None),
                ),
                and_(
                    WorkspaceStaff.staff_id.is_(None),
                    UserModel.id.is_not(None),
                ),
            ),
        )
    )
    rows = result.all()
    return [
        StaffResponse(
            id=membership.id, workspace_id=membership.workspace_id,
            staff_id=membership.staff_id, user_id=getattr(membership, "user_id", None),
            display_name=(staff.name if staff else user_record.display_name),
            email=(staff.email if staff else user_record.email),
            role=membership.role,
            added_by=getattr(membership, "added_by", None),
            added_at=getattr(membership, "added_at", None),
            expires_at=getattr(membership, "expires_at", None),
            status=getattr(membership, "status", None),
            created_at=membership.created_at,
        )
        for membership, staff, user_record in rows
    ]


# Workspace-role enum from RFC §5.2. User-linked memberships always require
# one of these roles; legacy business labels are not authorization roles.
_WORKSPACE_ROLES = {"owner", "editor", "contributor", "viewer"}


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
    linked_user_id = req.user_id or staff.user_id
    # A supplied user_id must belong to this entity — otherwise a foreign or
    # mismatched user_id would be written onto the membership row (a data-
    # integrity footgun for the membership lookups keyed on user_id).
    if req.user_id is not None:
        from packages.core.models.user import User as _User

        member = (await db.execute(
            select(_User.id).where(
                _User.id == req.user_id,
                _User.entity_id == user.entity_id,
                _User.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if not member:
            raise HTTPException(400, "user_id does not belong to this entity")
        if staff.user_id and staff.user_id != req.user_id:
            raise HTTPException(400, "user_id does not match the staff member")
    # Permission-v1 user-scoped assignments should use the canonical
    # workspace role enum. Legacy staff-scoped assignments historically used
    # business labels like "reviewer" / "lead"; keep those working.
    if linked_user_id and req.role not in _WORKSPACE_ROLES:
        raise HTTPException(
            400, f"Invalid or missing workspace role: {req.role}. "
            f"Expected one of {sorted(_WORKSPACE_ROLES)}"
        )
    membership_filters = [WorkspaceStaff.staff_id == req.staff_id]
    if linked_user_id:
        membership_filters.append(WorkspaceStaff.user_id == linked_user_id)
    existing_rows = list((await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            or_(*membership_filters),
        ).order_by(WorkspaceStaff.updated_at.desc(), WorkspaceStaff.created_at.desc())
    )).scalars().all())
    ws_staff = existing_rows[0] if existing_rows else None
    was_existing = ws_staff is not None
    if ws_staff:
        ws_staff.staff_id = req.staff_id
        ws_staff.user_id = linked_user_id
        ws_staff.role = req.role
        # Reassignment is a complete membership policy update. Omitting an
        # expiry means a permanent assignment and must clear any stale/expired
        # value left on the row.
        ws_staff.expires_at = req.expires_at
        # Reactivate if previously inactive
        if getattr(ws_staff, "status", None) != "active":
            ws_staff.status = "active"
    else:
        ws_staff = WorkspaceStaff(
            workspace_id=workspace_id,
            staff_id=req.staff_id,
            user_id=linked_user_id,
            role=req.role,
            expires_at=req.expires_at,
            added_by=user.id,
            added_at=datetime.now(UTC),
            status="active",
        )
        db.add(ws_staff)
    # Rolling-upgrade repair: collapse any pre-constraint duplicate rows while
    # this authorized member assignment is already being updated.
    for duplicate in existing_rows[1:]:
        await db.delete(duplicate)
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
    ws = await _require_workspace_manage(db, workspace_id, user)
    if ws.status != "active":
        raise HTTPException(
            409,
            f"Workspace status is {ws.status!r}; complete setup before enabling heartbeat",
        )
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
                ChannelConfig.owner_user_id == user.id,
                and_(
                    ChannelConfig.owner_user_id.is_(None),
                    ChannelConfig.workspace_id == workspace_id,
                ),
            ),
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


@router.get("/{workspace_id}/webchat/resources")
async def list_webchat_workspace_resources(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    document_cursor: Annotated[
        int,
        Query(ge=0, le=_WEBCHAT_DOCUMENT_CURSOR_LIMIT),
    ] = 0,
):
    """Return Workspace resources that may be deliberately published in Webchat.

    Document visibility is evaluated through the same client-visible policy
    used by public-agent RAG. Workflow definitions, steps, variables and
    binding config are intentionally omitted from this picker projection.
    """
    from packages.core.models.document import (
        Document,
        DocumentGroup,
        DocumentGroupMember,
    )
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.services.document_access import (
        public_agent_visible_document_ids_batched,
    )

    from packages.core.contracts.webchat_page import BrandModule
    from packages.core.services.webchat_page import public_workspace_name

    workspace = await _require_workspace_manage(db, workspace_id, user)
    entity = await db.get(Entity, user.entity_id)
    public_documents: list[dict[str, str]] = []
    next_document_cursor: int | None = document_cursor
    scanned_documents = 0
    while (
        len(public_documents) < _WEBCHAT_RESOURCE_LIMIT
        and scanned_documents < _WEBCHAT_DOCUMENT_SCAN_LIMIT
    ):
        batch_limit = min(
            _WEBCHAT_DOCUMENT_SCAN_BATCH,
            _WEBCHAT_DOCUMENT_SCAN_LIMIT - scanned_documents,
        )
        candidates = list((await db.scalars(
            select(Document)
            .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(
                Document.entity_id == user.entity_id,
                DocumentGroup.entity_id == user.entity_id,
                DocumentGroup.workspace_id == workspace_id,
                Document.is_trashed.is_(False),
            )
            .distinct()
            .order_by(Document.name.asc(), Document.id.asc())
            .offset(next_document_cursor)
            .limit(batch_limit + 1)
        )).unique().all())
        has_more_candidates = len(candidates) > batch_limit
        documents = candidates[:batch_limit]
        if not documents:
            next_document_cursor = None
            break
        public_document_ids = await public_agent_visible_document_ids_batched(
            db,
            documents,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
        )
        consumed = 0
        for document in documents:
            consumed += 1
            if str(document.id) not in public_document_ids:
                continue
            public_documents.append({
                "id": document.id,
                "name": str(document.name)[:160],
                # Exact content is resolved only for the candidate page sent
                # to the Review endpoint below.
                "body": "",
            })
            if len(public_documents) == _WEBCHAT_RESOURCE_LIMIT:
                break
        next_document_cursor += consumed
        scanned_documents += consumed
        if consumed < len(documents):
            break
        if not has_more_candidates:
            next_document_cursor = None
            break

    rows = (await db.execute(
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == user.entity_id,
            WorkflowBinding.workspace_id == workspace_id,
            WorkflowBinding.trigger_type == "manual",
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.entity_id == user.entity_id,
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
        .order_by(WorkflowBinding.name.asc(), WorkflowDefinition.name.asc())
        .limit(_WEBCHAT_RESOURCE_LIMIT)
    )).all()
    public_name = public_workspace_name(workspace)
    profile_values = {
        "name": public_name,
        "body": str(workspace.description or "")[:4000],
        "image_url": str(workspace.cover_image_url or ""),
        "items": [
            str(value)[:500]
            for value in (workspace.category, workspace.address)
            if value
        ][:8],
    }
    try:
        profile = ResolvedWorkspaceContent(**profile_values)
    except ValueError:
        profile = ResolvedWorkspaceContent(**{**profile_values, "image_url": ""})
    brand_values = {
        "id": "brand-default",
        "side": "left",
        "type": "brand",
        "name": public_name,
        "logo_url": str(getattr(entity, "logo_url", "") or ""),
        "website": "",
    }
    try:
        brand = BrandModule(**brand_values)
    except ValueError:
        brand = BrandModule(**{**brand_values, "logo_url": ""})
    return {
        "brand": {
            "name": brand.name,
            "logo_url": brand.logo_url,
            "website": brand.website,
        },
        "profile": {"id": "workspace", **profile.model_dump()},
        "documents": public_documents,
        "documents_next_cursor": next_document_cursor,
        "actions": [
            {
                "id": binding.id,
                "name": str(binding.name or workflow.name)[:160],
                "description": str(workflow.description or "")[:1000],
            }
            for binding, workflow in rows
        ],
    }


@router.post("/{workspace_id}/webchat/review", response_model=WebchatPage)
async def review_webchat_workspace_page(
    workspace_id: str,
    page: WebchatPage,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a candidate page through the exact public Workspace policy."""
    await _require_workspace_manage(db, workspace_id, user)
    from packages.core.services.webchat_page import resolve_workspace_webchat_page

    resolved = await resolve_workspace_webchat_page(
        db,
        value=page,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    if resolved is None:  # The request model already validates; stay fail-closed.
        raise HTTPException(422, "Invalid Webchat page")
    return resolved


@router.get(
    "/{workspace_id}/channels/{channel_binding_id}/webchat/review",
    response_model=WebchatPage,
)
async def review_saved_webchat_workspace_page(
    workspace_id: str,
    channel_binding_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve only the already-published page for a read-only reviewer."""
    from packages.core.models.document import Channel
    from packages.core.services.channel_bindings import channel_runtime_config
    from packages.core.services.webchat_page import resolve_workspace_webchat_page

    await _require_workspace_read(db, workspace_id, user)
    binding = await db.scalar(select(Channel).where(
        Channel.id == channel_binding_id,
        Channel.entity_id == user.entity_id,
        Channel.workspace_id == workspace_id,
        Channel.type == "webchat",
        Channel.status == "active",
    ))
    if binding is None:
        raise HTTPException(404, "Webchat channel not found")
    channel_config_id = str(
        (binding.config or {}).get("channel_config_id") or ""
    ).strip()
    if not channel_config_id:
        raise HTTPException(404, "Webchat channel not found")
    channel_config = await db.scalar(select(ChannelConfig).where(
        ChannelConfig.id == channel_config_id,
        ChannelConfig.entity_id == user.entity_id,
        ChannelConfig.channel_type == "webchat",
        ChannelConfig.status == "active",
        or_(
            ChannelConfig.workspace_id.is_(None),
            ChannelConfig.workspace_id == workspace_id,
        ),
    ))
    if channel_config is None:
        raise HTTPException(404, "Webchat channel not found")
    config = channel_runtime_config(channel_config, binding)
    resolved = await resolve_workspace_webchat_page(
        db,
        value=config.get("public_page"),
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    return resolved or WebchatPage()


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
    request_config = await _resolved_webchat_page_config(
        db,
        _normalized_channel_config(req.config) if req.config else {},
        entity_id=user.entity_id,
        workspace_id=workspace_id,
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

    resolved_agent_id = sub.agent_id if sub else req.agent_id

    channel_config = None
    if req.channel_config_id:
        channel_config = (await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.id == req.channel_config_id,
                ChannelConfig.entity_id == user.entity_id,
                ChannelConfig.status == "active",
                or_(
                    ChannelConfig.owner_user_id == user.id,
                    and_(
                        ChannelConfig.owner_user_id.is_(None),
                        ChannelConfig.workspace_id == workspace_id,
                    ),
                ),
                or_(
                    ChannelConfig.workspace_id.is_(None),
                    ChannelConfig.workspace_id == workspace_id,
                ),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if not channel_config:
            raise HTTPException(404, "Channel config not found")
    else:
        channel_type = (req.channel_type or "webchat").strip()
        if channel_type != "webchat":
            raise HTTPException(400, "channel_config_id is required for external channels")
        public_token = generate_ulid()
        cfg = dict(request_config)
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
    if request_config:
        binding_config.update(request_config)
    existing = (await db.execute(
        select(Channel).where(
            Channel.entity_id == user.entity_id,
            Channel.workspace_id == workspace_id,
            Channel.config["channel_config_id"].astext == channel_config.id,
        )
    )).scalar_one_or_none()
    if channel_config.channel_type == "slack":
        other_binding = (await db.execute(
            select(Channel.id).where(
                Channel.entity_id == user.entity_id,
                Channel.type == "slack",
                Channel.status == "active",
                Channel.config["channel_config_id"].astext == channel_config.id,
                *([Channel.id != existing.id] if existing else []),
            ).limit(1)
        )).scalar_one_or_none()
        if other_binding is not None:
            raise HTTPException(
                409,
                "Slack installation already has an active Agent binding",
            )
    if (
        resolved_agent_id
        and (existing is None or resolved_agent_id != existing.agent_id)
        and not is_master_agent(resolved_agent_id)
    ):
        await lock_reusable_resource_references(
            db,
            entity_id=user.entity_id,
            agent_ids=(resolved_agent_id,),
        )
    if existing:
        existing.agent_id = resolved_agent_id
        existing.agent_subscription_id = sub.id if sub else None
        existing.name = req.name or existing.name or channel_config.name or channel_config.channel_type
        existing.config = binding_config
        existing.status = "active"
        existing.user_id = channel_config.owner_user_id
        channel_binding = existing
    else:
        channel_binding = Channel(
            id=generate_ulid(),
            entity_id=user.entity_id,
            user_id=channel_config.owner_user_id,
            workspace_id=workspace_id,
            type=channel_config.channel_type,
            name=req.name or channel_config.name or channel_config.channel_type,
            agent_id=resolved_agent_id,
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
        agent_id=resolved_agent_id,
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
        ).execution_options(populate_existing=True)
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
            ChannelConfig.channel_type == binding.type,
            or_(
                ChannelConfig.workspace_id.is_(None),
                ChannelConfig.workspace_id == workspace_id,
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not channel_config:
        raise HTTPException(404, "Channel config not found")

    # A missing legacy owner may be repaired, but an ownership transfer
    # requires the new account owner to explicitly authorize the binding.
    if (
        binding.user_id is not None
        and binding.user_id != channel_config.owner_user_id
        and user.id != channel_config.owner_user_id
    ):
        raise HTTPException(403, "Channel account ownership changed; ask its owner to reconnect it")

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

    resolved_agent_id = sub.agent_id if sub else req.agent_id
    if (
        routing_requested
        and resolved_agent_id
        and resolved_agent_id != binding.agent_id
        and not is_master_agent(resolved_agent_id)
    ):
        await lock_reusable_resource_references(
            db,
            entity_id=user.entity_id,
            agent_ids=(resolved_agent_id,),
        )

    req_config = await _resolved_webchat_page_config(
        db,
        _normalized_channel_config(req.config) if req.config else {},
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    # PATCHing page content must not reset the channel's existing language.
    if "language" not in req.config and "locale" not in req.config:
        req_config.pop("language", None)
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
        binding.agent_id = resolved_agent_id
        binding.agent_subscription_id = sub.id if sub else None
    binding.config = binding_config
    binding.user_id = channel_config.owner_user_id

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
        agent_id=resolved_agent_id if routing_requested else binding.agent_id,
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
            oauth_account_is_runtime_usable_clause(),
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

_WORKSPACE_GROUP_DEFAULT_KIND = WorkspaceDocumentGroupKind.DEFAULT_COLLECTION.value
_WORKSPACE_GROUP_FOLDER_KIND = WorkspaceDocumentGroupKind.KNOWLEDGE_NET.value
_WORKSPACE_GROUP_FILE_BUCKET_KIND = WorkspaceDocumentGroupKind.FILE_BUCKET.value
_WORKSPACE_DEFAULT_COLLECTION_NAME = "Workspace Knowledge"
_WORKSPACE_KNOWLEDGE_REVALIDATION_BATCH_SIZE = 500


def _workspace_group_settings(group: DocumentGroup) -> dict:
    return dict(group.settings or {})


def _workspace_group_kind(group: DocumentGroup) -> str:
    settings = _workspace_group_settings(group)
    if settings.get("workspace_file_bucket"):
        return _WORKSPACE_GROUP_FILE_BUCKET_KIND
    if settings.get("default_collection"):
        return _WORKSPACE_GROUP_DEFAULT_KIND
    kind = str(settings.get("kind") or _WORKSPACE_GROUP_FOLDER_KIND)
    return (
        _WORKSPACE_GROUP_FOLDER_KIND
        if kind == WorkspaceDocumentGroupKind.LEGACY_KNOWLEDGE_FOLDER
        else kind
    )


async def _lock_workspace_knowledge_import_authority(
    db: AsyncSession,
    *,
    workspace_id: str,
    user: User,
) -> str:
    """Serialize revocation of every row that can authorize a folder import."""
    from packages.core.models.permission import (
        GrantStatus,
        ResourceGrant,
        ResourceType,
        SubjectType,
    )
    from packages.core.models.staff import Staff, StaffRole
    from packages.core.models.user import UserMembership
    from packages.core.permissions import effective_user_role_name

    await db.execute(
        select(User.id)
        .where(User.id == user.id, User.deleted_at.is_(None))
        .with_for_update()
    )
    await db.execute(
        select(UserMembership)
        .where(
            UserMembership.user_id == user.id,
            UserMembership.entity_id == user.entity_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    staff_rows = list((await db.execute(
        select(Staff)
        .where(
            Staff.user_id == user.id,
            Staff.entity_id == user.entity_id,
        )
        .order_by(Staff.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )).scalars().all())
    role_ids = {staff.role_id for staff in staff_rows if staff.role_id}
    if role_ids:
        await db.execute(
            select(StaffRole)
            .where(
                StaffRole.id.in_(role_ids),
                StaffRole.entity_id == user.entity_id,
            )
            .order_by(StaffRole.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    await db.execute(
        select(WorkspaceStaff)
        .where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.user_id == user.id,
        )
        .order_by(WorkspaceStaff.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )

    subject_ids = {
        user.id,
        *(
            staff.id
            for staff in staff_rows
            if staff.status == "active" and staff.deleted_at is None
        ),
    }
    await db.execute(
        select(ResourceGrant)
        .where(
            ResourceGrant.entity_id == user.entity_id,
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.subject_id.in_(subject_ids),
            ResourceGrant.resource_type.in_([
                ResourceType.DOCUMENT,
                ResourceType.DOCUMENT_FOLDER,
            ]),
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
        .order_by(ResourceGrant.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return await effective_user_role_name(db, user)


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
    ws = await _require_workspace_manage(db, workspace_id, user)
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Knowledge Net name is required")
    kind = (req.kind or _WORKSPACE_GROUP_FOLDER_KIND).strip() or _WORKSPACE_GROUP_FOLDER_KIND
    if kind == WorkspaceDocumentGroupKind.LEGACY_KNOWLEDGE_FOLDER:
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
    group = await create_workspace_knowledge_group(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        name=name,
        kind=kind,
        purpose=req.purpose or "",
    )
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
        settings["kind"] = (
            _WORKSPACE_GROUP_FOLDER_KIND
            if next_kind == WorkspaceDocumentGroupKind.LEGACY_KNOWLEDGE_FOLDER
            else next_kind
        )
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
    from packages.core.models.permission import Capability
    from packages.core.services.document_access import (
        get_visible_document,
        partition_documents_by_capability,
    )

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

    visible_documents = []
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
        visible_documents.append(doc)
    manageable_documents, _ = await partition_documents_by_capability(
        db,
        visible_documents,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        required_capability=Capability.MANAGE_METADATA,
        workspace_id=workspace_id,
    )
    manageable_by_id = {document.id: document for document in manageable_documents}

    added = 0
    for doc_id in doc_ids:
        doc = manageable_by_id.get(doc_id)
        if doc is None:
            if doc_id not in skipped:
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


@router.post(
    "/{workspace_id}/documents/folders/{folder_id}",
    response_model=WorkspaceKnowledgeFolderAddResponse,
)
async def add_knowledge_folder_to_workspace(
    workspace_id: str,
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Attach every visible document in a Knowledge folder subtree.

    Workspace Knowledge Nets are flat collections, so one source folder maps
    to one same-named Net. Repeating the operation reuses that Net and adds
    only documents that are not already members.
    """
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.permission import Capability
    from packages.core.services.document_access import (
        list_visible_document_ids_batched,
        partition_documents_by_capability,
        user_can_read_folder,
        user_has_folder_capability,
    )
    from packages.core.services.document_service import add_documents_to_group
    from packages.core.services.knowledge_visibility import (
        is_user_visible_folder_path,
    )

    await _require_workspace_manage(db, workspace_id, user)
    source_folder = (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.id == folder_id,
            DocumentFolder.entity_id == user.entity_id,
        )
    )).scalar_one_or_none()
    if source_folder is None:
        raise HTTPException(404, "Knowledge folder not found")

    path_parts: list[str] = []
    current = source_folder
    path_seen: set[str] = set()
    while current and current.id not in path_seen:
        path_seen.add(current.id)
        path_parts.append(current.name)
        if not await user_can_read_folder(
            db,
            current,
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
        ):
            raise HTTPException(404, "Knowledge folder not found")
        if not current.parent_id:
            current = None
            continue
        current = (await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.id == current.parent_id,
                DocumentFolder.entity_id == user.entity_id,
            )
        )).scalar_one_or_none()
    if not is_user_visible_folder_path("/".join(reversed(path_parts))):
        raise HTTPException(404, "Knowledge folder not found")
    can_manage_source_folder = (
        source_folder.owner_id == user.id
        or await user_is_effective_entity_admin(db, user)
        or await user_has_folder_capability(
            db,
            entity_id=user.entity_id,
            folder_id=source_folder.id,
            user_id=user.id,
            capabilities={Capability.MANAGE_METADATA},
        )
    )
    if not can_manage_source_folder:
        raise HTTPException(
            403,
            "Folder metadata management permission is required",
        )

    folder_tree = select(DocumentFolder.id).where(
        DocumentFolder.id == source_folder.id,
        DocumentFolder.entity_id == user.entity_id,
    ).cte("knowledge_folder_tree", recursive=True)
    folder_tree = folder_tree.union(
        select(DocumentFolder.id)
        .join(folder_tree, DocumentFolder.parent_id == folder_tree.c.id)
        .where(DocumentFolder.entity_id == user.entity_id)
    )
    subtree_ids = set((await db.execute(
        select(folder_tree.c.id)
    )).scalars().all())

    try:
        document_ids = await list_visible_document_ids_batched(
            db,
            user.entity_id,
            user_id=user.id,
            role=user.role,
            folder_ids=subtree_ids,
            required_capability=Capability.MANAGE_METADATA,
        )
    except PermissionError as exc:
        raise HTTPException(
            403,
            "Every imported document must allow metadata management",
        ) from exc

    # Serialize only the same-folder Net lookup/create and member upserts. The
    # recursive permission scan above can be large and must not block unrelated
    # Workspace management for its entire duration.
    locked_workspace_id = (await db.execute(
        select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        ).with_for_update()
    )).scalar_one_or_none()
    if locked_workspace_id is None:
        raise HTTPException(404, "Workspace not found")
    # The recursive scan intentionally happens outside the Workspace lock.
    # Lock every row that can authorize this actor before re-checking, so a
    # concurrent role, membership, or grant revocation commits after this
    # import rather than between its authorization check and member writes.
    effective_entity_role = await _lock_workspace_knowledge_import_authority(
        db,
        workspace_id=workspace_id,
        user=user,
    )
    if not await user_can_manage_workspace(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        entity_role=effective_entity_role,
    ):
        raise HTTPException(
            403,
            "Only an entity owner/admin or the workspace owner can manage this workspace",
        )
    source_folder = (await db.execute(
        select(DocumentFolder)
        .where(
            DocumentFolder.id == folder_id,
            DocumentFolder.entity_id == user.entity_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )).scalar_one_or_none()
    if source_folder is None:
        raise HTTPException(404, "Knowledge folder not found")
    current = source_folder
    path_parts = []
    path_seen = set()
    while current and current.id not in path_seen:
        path_seen.add(current.id)
        path_parts.append(current.name)
        if not await user_can_read_folder(
            db,
            current,
            entity_id=user.entity_id,
            user_id=user.id,
            role=effective_entity_role,
        ):
            raise HTTPException(404, "Knowledge folder not found")
        if not current.parent_id:
            current = None
            continue
        current = (await db.execute(
            select(DocumentFolder)
            .where(
                DocumentFolder.id == current.parent_id,
                DocumentFolder.entity_id == user.entity_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )).scalar_one_or_none()
    if not is_user_visible_folder_path("/".join(reversed(path_parts))):
        raise HTTPException(404, "Knowledge folder not found")
    can_manage_source_folder = (
        source_folder.owner_id == user.id
        or effective_entity_role in {"owner", "admin"}
        or await user_has_folder_capability(
            db,
            entity_id=user.entity_id,
            folder_id=source_folder.id,
            user_id=user.id,
            capabilities={Capability.MANAGE_METADATA},
        )
    )
    if not can_manage_source_folder:
        raise HTTPException(
            403,
            "Folder metadata management permission is required",
        )

    current_folder_tree = select(DocumentFolder.id).where(
        DocumentFolder.id == source_folder.id,
        DocumentFolder.entity_id == user.entity_id,
    ).cte("current_knowledge_folder_tree", recursive=True)
    current_folder_tree = current_folder_tree.union(
        select(DocumentFolder.id)
        .join(
            current_folder_tree,
            DocumentFolder.parent_id == current_folder_tree.c.id,
        )
        .where(DocumentFolder.entity_id == user.entity_id)
    )
    # Folder moves/deletes update these rows, so lock the current subtree in a
    # deterministic order before validating its original document snapshot.
    await db.execute(
        select(DocumentFolder)
        .where(
            DocumentFolder.entity_id == user.entity_id,
            DocumentFolder.id.in_(select(current_folder_tree.c.id)),
        )
        .order_by(DocumentFolder.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if document_ids:
        manageable_document_ids: set[str] = set()
        for start in range(
            0,
            len(document_ids),
            _WORKSPACE_KNOWLEDGE_REVALIDATION_BATCH_SIZE,
        ):
            document_batch = document_ids[
                start : start + _WORKSPACE_KNOWLEDGE_REVALIDATION_BATCH_SIZE
            ]
            current_documents = list((await db.execute(
                select(Document)
                .where(
                    Document.id.in_(document_batch),
                    Document.entity_id == user.entity_id,
                    Document.folder_id.in_(select(current_folder_tree.c.id)),
                    Document.is_trashed.is_(False),
                )
                .order_by(Document.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )).scalars().all())
            manageable_documents, _ = await partition_documents_by_capability(
                db,
                current_documents,
                entity_id=user.entity_id,
                user_id=user.id,
                role=effective_entity_role,
                required_capability=Capability.MANAGE_METADATA,
            )
            manageable_document_ids.update(
                document.id for document in manageable_documents
            )
        if manageable_document_ids != set(document_ids):
            raise HTTPException(
                403,
                "Every imported document must allow metadata management",
            )
    groups = list((await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == user.entity_id,
            DocumentGroup.workspace_id == workspace_id,
        )
    )).scalars().all())
    group = next(
        (
            candidate
            for candidate in groups
            if isinstance((candidate.settings or {}).get("knowledge_folder_source"), dict)
            and (candidate.settings or {})["knowledge_folder_source"].get("folder_id") == source_folder.id
        ),
        None,
    )
    created = group is None
    if group is None:
        group = DocumentGroup(
            id=generate_ulid(),
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            name=source_folder.name,
            settings={
                "kind": _WORKSPACE_GROUP_FOLDER_KIND,
                "scope": "workspace",
                "purpose": f"Documents from the Knowledge folder {source_folder.name}.",
                "user_manageable": True,
                "knowledge_folder_source": {
                    "folder_id": source_folder.id,
                    "mode": "recursive_snapshot",
                },
            },
        )
        db.add(group)
        await db.flush()

    added = await add_documents_to_group(
        db,
        document_ids,
        group.id,
        entity_id=user.entity_id,
    )
    await db.commit()
    if created or added:
        await _mark_workspace_knowledge_changed(user.entity_id, workspace_id)

    return WorkspaceKnowledgeFolderAddResponse(
        group_id=group.id,
        group_name=group.name,
        created=created,
        added=added,
        existing=len(document_ids) - added,
        total=len(document_ids),
    )


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
    """One-click "Try a demo" — provisions a complete Workspace simulation
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
                "Workspace simulation: memory seeding failed (Workspace still usable)",
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


# ── Blueprint upgrade ────────────────────────────────────────────────
# Three endpoints, because the three acts carry different risk: reading the
# plan is safe, applying overwrites content, reverting undoes exactly what
# the last apply overwrote. The operator confirms between the first and the
# second — nothing upgrades on its own.


class BlueprintConflictResolutionRequest(BaseModel):
    kind: Literal["skill", "agent", "workflow"]
    slug: str = Field(min_length=1, max_length=200)
    resolution: Literal["keep_yours", "use_blueprint"]
    expected_revision: int = Field(ge=1)


class BlueprintUpgradeVariableValuesRequest(BaseModel):
    variable_values: dict[str, Any] = Field(default_factory=dict, max_length=50)
    channel_config_ids: dict[str, str] = Field(default_factory=dict, max_length=50)


class BlueprintUpgradePreviewRequest(BlueprintUpgradeVariableValuesRequest):
    pass


class BlueprintUpgradeApplyRequest(BlueprintUpgradeVariableValuesRequest):
    expected_blueprint_fingerprint: str = Field(min_length=1, max_length=128)
    conflict_resolutions: list[BlueprintConflictResolutionRequest] = Field(
        default_factory=list,
        max_length=200,
    )


BLUEPRINT_UPGRADE_PROTOCOL_VERSION = 2


async def _blueprint_payload_for(db: AsyncSession, ws) -> tuple | None:
    """The payload this workspace would upgrade toward — built-in or
    marketplace, resolved the same way the list does."""
    return (await _blueprint_payloads_for(db, [ws])).get(ws.id)


async def _require_blueprint_upgrade_access(
    db: AsyncSession,
    *,
    user: User,
    resolved_blueprint_id: str | None,
) -> None:
    """Apply the same paid-content gate used by Blueprint installation."""
    if not resolved_blueprint_id:
        return

    from packages.core.models.blueprint import WorkspaceBlueprint
    from packages.core.constants.blueprints import BlueprintPurchaseStatus
    from packages.core.models.blueprint_purchase import BlueprintPurchase
    from packages.core.services.marketplace_billing import (
        MarketplacePaidPlanRequiredError,
        require_paid_marketplace_plan,
    )

    row = await db.get(WorkspaceBlueprint, resolved_blueprint_id)
    if (
        row is None
        or row.entity_id == user.entity_id
        or (row.price_cents or 0) <= 0
    ):
        return

    try:
        await require_paid_marketplace_plan(
            db,
            entity_id=user.entity_id,
            blueprint_id=row.id,
        )
    except MarketplacePaidPlanRequiredError as exc:
        raise HTTPException(402, detail=exc.detail) from exc

    purchase_id = (await db.execute(
        select(BlueprintPurchase.id).where(
            BlueprintPurchase.blueprint_id == row.id,
            BlueprintPurchase.buyer_entity_id == user.entity_id,
            BlueprintPurchase.status == BlueprintPurchaseStatus.COMPLETED.value,
        ).limit(1)
    )).scalar_one_or_none()
    if purchase_id is None:
        raise HTTPException(402, "purchase required to upgrade this blueprint")


async def _blueprint_upgrade_setup_preflight(
    db: AsyncSession,
    *,
    payload: dict | None,
    user: User,
    workspace_id: str,
    selected_channel_config_ids: dict[str, str] | None = None,
) -> dict:
    if not payload:
        return {"ready": True, "blocking_count": 0, "requirements": []}
    from packages.core.blueprints.setup_preflight import (
        BlueprintSetupPreflightError,
        BlueprintSetupPreflightFactory,
    )

    try:
        result = await BlueprintSetupPreflightFactory.from_payload(
            db,
            payload=payload,
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=workspace_id,
            selected_channel_config_ids=selected_channel_config_ids,
        )
    except BlueprintSetupPreflightError as exc:
        logger.warning("Blueprint upgrade preflight rejected a payload", exc_info=True)
        raise HTTPException(
            400,
            "Blueprint setup requirements could not be evaluated.",
        ) from exc
    requirements = [{
        "kind": item.kind.value,
        "provider": item.provider,
        "label": item.label,
        "required": item.required,
        "ready": item.ready,
        "reason": item.reason,
        "purpose": item.purpose,
        "setup_kind": item.setup_kind,
        "scope": item.scope,
        "config_fields_to_set": list(item.config_fields_to_set),
        "requirement_key": item.requirement_key,
        "resource_id": item.resource_id,
        "resource_options": [
            {"id": option.id, "label": option.label}
            for option in item.resource_options
        ],
    } for item in result.requirements]
    return {
        "ready": result.ready,
        "blocking_count": len(result.blocking_requirements),
        "requirements": requirements,
    }


def _blueprint_upgrade_variable_inputs(
    *,
    workspace: Workspace,
    payload: dict | None,
    supplied: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    declarations = {
        str(item.get("key")): item
        for item in ((payload or {}).get("contract") or {}).get("variables") or []
        if isinstance(item, dict) and item.get("key")
    }
    saved = (workspace.settings or {}).get("blueprint_personalization")
    values = {
        key: value
        for key, value in (saved if isinstance(saved, dict) else {}).items()
        if key in declarations
    }
    values.update(dict(supplied or {}))
    resolved_keys = []
    for key, declaration in declarations.items():
        value = values.get(key, declaration.get("default"))
        if value is not None and not (
            isinstance(value, str) and not value.strip()
        ):
            resolved_keys.append(key)
    return values, resolved_keys


async def _preview_blueprint_upgrade(
    *,
    workspace_id: str,
    variable_values: dict[str, Any],
    channel_config_ids: dict[str, str] | None,
    user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    ws = await _require_workspace_read(db, workspace_id, user)
    from apps.api.routers.blueprints import _payload_setup_preview
    from packages.core.blueprints.installer import (
        InstallError,
        resolve_install_variables,
    )
    from packages.core.blueprints.upgrade import plan

    resolved = await _blueprint_payload_for(db, ws)
    source_payload, _current_version, resolved_id = (
        resolved if resolved else (None, None, None)
    )
    await _require_blueprint_upgrade_access(
        db,
        user=user,
        resolved_blueprint_id=resolved_id,
    )
    effective_payload = source_payload
    variables_ready = True
    resolved_variable_keys: list[str] = []
    if isinstance(source_payload, dict):
        merged_values, resolved_variable_keys = _blueprint_upgrade_variable_inputs(
            workspace=ws,
            payload=source_payload,
            supplied=variable_values,
        )
        try:
            effective_payload, _personalization = resolve_install_variables(
                source_payload,
                merged_values,
            )
        except InstallError:
            variables_ready = False
            # An unresolved raw payload must never be compared with already
            # materialized Workspace content. The variable form remains
            # available, but component planning waits for valid values.
            effective_payload = None
    result = await plan(
        db,
        workspace=ws,
        payload=effective_payload,
        source_payload=source_payload,
        source_blueprint_id=resolved_id,
    )
    setup_preflight = (
        await _blueprint_upgrade_setup_preflight(
            db,
            payload=effective_payload,
            user=user,
            workspace_id=ws.id,
            selected_channel_config_ids=channel_config_ids,
        )
        if variables_ready
        else {"ready": True, "blocking_count": 0, "requirements": []}
    )
    return {
        "upgrade_protocol_version": BLUEPRINT_UPGRADE_PROTOCOL_VERSION,
        "setup_preflight": setup_preflight,
        "setup_preview": _payload_setup_preview(source_payload).model_dump(),
        "variables_ready": variables_ready,
        "resolved_variable_keys": resolved_variable_keys,
        **result,
    }


@router.get("/{workspace_id}/blueprint/upgrade")
async def preview_blueprint_upgrade(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What an upgrade would change. Reads only — this is what gets confirmed."""
    return await _preview_blueprint_upgrade(
        workspace_id=workspace_id,
        variable_values={},
        channel_config_ids={},
        user=user,
        db=db,
    )


@router.post("/{workspace_id}/blueprint/upgrade/preview")
async def preview_blueprint_upgrade_with_variables(
    workspace_id: str,
    req: BlueprintUpgradePreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rebuild the reviewed plan with typed one-shot Blueprint values."""
    return await _preview_blueprint_upgrade(
        workspace_id=workspace_id,
        variable_values=req.variable_values,
        channel_config_ids=req.channel_config_ids,
        user=user,
        db=db,
    )


@router.post("/{workspace_id}/blueprint/upgrade/v2")
@router.post("/{workspace_id}/blueprint/upgrade")
async def apply_blueprint_upgrade(
    workspace_id: str,
    req: BlueprintUpgradeApplyRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply the reviewed update. Workspace edits stay untouched unless the
    operator explicitly chose the Blueprint version; overwritten values are
    retained so the update can be undone."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    from packages.core.blueprints.upgrade import (
        BlueprintUpgradeAccessDeniedError,
        BlueprintUpgradeIncompleteError,
        BlueprintUpgradePlanChangedError,
        apply,
    )

    resolved = (await _blueprint_payloads_for(db, [ws])).get(ws.id)
    source_payload, current_version, resolved_id = (
        resolved if resolved else (None, None, None)
    )
    await _require_blueprint_upgrade_access(
        db,
        user=user,
        resolved_blueprint_id=resolved_id,
    )
    from packages.core.blueprints.installer import (
        InstallError,
        resolve_install_variables,
    )

    payload = source_payload
    personalization: dict[str, Any] | None = None
    try:
        if isinstance(source_payload, dict):
            merged_values, _resolved_keys = _blueprint_upgrade_variable_inputs(
                workspace=ws,
                payload=source_payload,
                supplied=req.variable_values,
            )
            payload, personalization = resolve_install_variables(
                source_payload,
                merged_values,
            )
    except InstallError as exc:
        raise HTTPException(
            status_code=400,
            detail="Blueprint personalization values are incomplete or invalid.",
        ) from exc
    setup_preflight = await _blueprint_upgrade_setup_preflight(
        db,
        payload=payload,
        user=user,
        workspace_id=ws.id,
        selected_channel_config_ids=req.channel_config_ids,
    )
    if not setup_preflight["ready"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blueprint_setup_required",
                "message": (
                    "Connect all newly required Blueprint integrations, channels, "
                    "and sessions before upgrading the Workspace."
                ),
                "preflight": setup_preflight,
            },
        )
    try:
        result = await apply(
            db,
            workspace=ws,
            payload=payload,
            source_payload=source_payload,
            personalization=personalization,
            by_user_id=user.id,
            current_version=current_version,
            source_blueprint_id=resolved_id,
            expected_blueprint_fingerprint=req.expected_blueprint_fingerprint,
            conflict_resolutions=[
                item.model_dump() for item in req.conflict_resolutions
            ],
            channel_config_ids={
                requirement["requirement_key"]: requirement["resource_id"]
                for requirement in setup_preflight["requirements"]
                if requirement.get("requirement_key") and requirement.get("resource_id")
            } | req.channel_config_ids,
            actor=user,
            require_complete_workspace=True,
        )
    except BlueprintUpgradeAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except BlueprintUpgradeIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BlueprintUpgradePlanChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return {
        "upgrade_protocol_version": BLUEPRINT_UPGRADE_PROTOCOL_VERSION,
        **result,
    }


@router.post("/{workspace_id}/blueprint/revert")
async def revert_blueprint_upgrade(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Put back what the most recent upgrade overwrote."""
    ws = await _require_workspace_manage(db, workspace_id, user)
    from packages.core.blueprints.upgrade import (
        BlueprintUpgradeAccessDeniedError,
        BlueprintUpgradePlanChangedError,
        revert,
    )

    try:
        result = await revert(
            db,
            workspace=ws,
            by_user_id=user.id,
            actor=user,
        )
    except BlueprintUpgradeAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except BlueprintUpgradePlanChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return result
