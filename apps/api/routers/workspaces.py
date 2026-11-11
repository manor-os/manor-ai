"""Workspace endpoints — CRUD, operating model, agent mappings, activity, setup."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
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
from packages.core.models.user import Entity, OAuthAccount, User
from packages.core.models.event import EventLog
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Document, DocumentFolder, DocumentGroup, Integration
from packages.core.permissions import (
    Permission,
    effective_user_has_permission,
    user_is_effective_entity_admin,
)
from packages.core.services.entity_service import (
    ProtectedWorkspaceSettingsError,
    WORKSPACE_PURGE_GRACE_DAYS,
    list_workspaces, get_workspace, create_workspace, update_workspace,
    soft_delete_workspace, restore_workspace, list_trashed_workspaces, purge_workspace,
)
from packages.core.services.workspace_dashboard_service import (
    get_workspace_stats,
    get_workspace_custom_field_summary,
)
from packages.core.services.workspace_runtime import (
    sync_workspace_runtime_schedules,
)
from apps.api.collaborator_boundary import collaborator_scope
from packages.core.services.collaborator_scope import require_collaborator_references
from packages.core.services.workspace_access import (
    ensure_workspace_owner_membership,
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    manageable_workspace_ids_for_user,
    owned_workspace_ids_for_user,
    set_link_columns,
    settings_with_default_workspace_access,
    user_can_manage_workspace,
)
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    resolve_current_user_actor,
)
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
    list_documents,
    mark_workspace_knowledge_changed,
)
from packages.core.services.document_access import DocumentAccessContext
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_references,
)
from packages.core.services.scheduled_run_lifecycle import (
    ScheduledAgentLineageDeletionBlockedError,
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


def _connection_account_label(
    *,
    channel_config: ChannelConfig,
    integration: Integration | None = None,
    oauth_account: OAuthAccount | None = None,
) -> str:
    source: dict[str, Any] = {}
    if integration is not None and isinstance(integration.config, dict):
        source = integration.config
    elif oauth_account is not None and isinstance(oauth_account.profile, dict):
        source = oauth_account.profile
    keys = (
        (
            "email",
            "from_address",
            "from_email",
            "username",
            "account_name",
            "display_name",
            "name",
        )
        if channel_config.channel_type == "email"
        else (
            "account_name",
            "team_name",
            "guild_name",
            "display_name",
            "name",
            "email",
            "username",
            "from_address",
            "from_email",
            "phone_number",
            "user",
        )
    )
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    if oauth_account is not None:
        provider_user_id = str(oauth_account.provider_user_id or "").strip()
        if provider_user_id:
            return provider_user_id
    return str(channel_config.name or "").strip()


async def _workspace_channel_connection_metadata(
    db: AsyncSession,
    channel_configs: list[ChannelConfig],
    *,
    entity_id: str,
) -> dict[str, dict[str, str | None]]:
    """Return credential-free provider/account labels for Channel binding UI."""
    integration_ids = {
        row.credential_source_id
        for row in channel_configs
        if row.credential_source_kind == "integration" and row.credential_source_id
    }
    oauth_account_ids = {
        row.credential_source_id
        for row in channel_configs
        if row.credential_source_kind == "oauth_account" and row.credential_source_id
    }
    integrations = {
        row.id: row
        for row in (
            await db.execute(
                select(Integration).where(
                    Integration.id.in_(integration_ids),
                    Integration.entity_id == entity_id,
                )
            )
        ).scalars().all()
    } if integration_ids else {}
    oauth_accounts = {
        row.id: row
        for row in (
            await db.execute(select(OAuthAccount).where(OAuthAccount.id.in_(oauth_account_ids)))
        ).scalars().all()
    } if oauth_account_ids else {}

    metadata: dict[str, dict[str, str | None]] = {}
    for channel_config in channel_configs:
        integration = integrations.get(channel_config.credential_source_id)
        oauth_account = oauth_accounts.get(channel_config.credential_source_id)
        if oauth_account is not None and oauth_account.user_id != channel_config.owner_user_id:
            oauth_account = None
        integration_provider = (
            integration.provider
            if integration is not None
            else oauth_account.provider
            if oauth_account is not None
            else channel_config.channel_type
        )
        metadata[channel_config.id] = {
            "credential_source_kind": channel_config.credential_source_kind,
            "credential_source_id": channel_config.credential_source_id,
            "integration_provider": integration_provider,
            "account_label": _connection_account_label(
                channel_config=channel_config,
                integration=integration,
                oauth_account=oauth_account,
            ),
        }
    return metadata


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
    last_chat_at: datetime | None = None
    created_by_user_id: str | None = None
    created_by_name: str | None = None
    created_by_email: str | None = None
    created_by_avatar_url: str | None = None
    can_manage: bool = False
    can_delete: bool = False
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


class WorkspaceRestoreUnmappedAgent(BaseModel):
    agent_id: str
    agent_name: str | None = None
    service_key: str | None = None


class WorkspaceRestoreResponse(WorkspaceResponse):
    #: Mappings dropped on restore because their Agent was deleted while the
    #: Workspace was in the trash; those service roles are now unassigned.
    unmapped_agents: list[WorkspaceRestoreUnmappedAgent] = Field(default_factory=list)


class WorkspaceSetupStatusResponse(BaseModel):
    ready: bool
    status: str
    summary: str
    incomplete_checks: list[dict[str, Any]] = Field(default_factory=list)
    automatic_setup_task_id: str | None = None
    automatic_setup_state: Literal[
        "pending", "running", "retrying", "failed"
    ] | None = None


class WorkspaceSetupInputRequest(BaseModel):
    check_key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


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
    avatar_url: str | None = None
    role: str | None = None
    added_by: str | None = None
    added_at: datetime | None = None
    expires_at: datetime | None = None
    status: str | None = None
    created_at: datetime | None = None
    # The grant role behind a seat derived from external collaboration
    # ("provider_trial" / "maintainer"); None for the customer's own members.
    collaboration_role: str | None = None
    # The seat belongs to the provider team of the delivery that installed the Workspace.
    collaboration_team: bool = False


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
    new_service: ServiceRequest | None = None


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
    allowed_patch_operations: list[str] | None = None


class OperationPatchRequest(BaseModel):
    patches: list[dict[str, Any]] = Field(default_factory=list)
    allowed_patch_operations: list[str] | None = None


class AppViewDesignRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=4000)
    feedback: str | None = Field(default=None, max_length=4000)
    # The earlier design this one replaces (it is discarded).
    draft_id: str | None = None
    # Who the page is for and what they act on first, as the person chose it.
    audience: str | None = Field(default=None, max_length=1000)


class OperationApplyRequest(BaseModel):
    user_confirmation: bool = True
    expected_review_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    allowed_patch_operations: list[str] | None = None


class SetupTurnRequest(BaseModel):
    session_id: str | None = None
    message: str


class SetupFinalizeRequest(BaseModel):
    session_id: str


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _deletable_workspace_ids(
    db: AsyncSession, user: User, *, manageable_ids: set[str],
) -> set[str]:
    """Workspace owners and authorized administrators may delete scoped targets."""
    if await user_is_effective_entity_admin(db, user) and await effective_user_has_permission(
        db, user, Permission.WORKSPACES_DELETE
    ):
        return manageable_ids
    return await owned_workspace_ids_for_user(db, workspace_ids=manageable_ids, user_id=user.id, lifecycle=True)


async def _restorable_workspace_ids(
    db: AsyncSession,
    workspaces: list[Workspace],
    *,
    credential: AuthenticatedUserCredential,
) -> set[str]:
    """Authorize recovery without making deleted Workspaces readable."""
    actor = await resolve_current_user_actor(db, credential)
    if actor is None:
        raise HTTPException(404, "Workspace not found")
    # Recovery belongs to retained Workspace owners, independently of their
    # organization role, and to existing global administrators.
    return await manageable_workspace_ids_for_user(
        db,
        workspaces=workspaces,
        entity_id=actor.entity_id,
        user_id=actor.user_id,
        entity_role=actor.role,
        include_deleted=True,
        lifecycle=True,
    )


async def _cancel_unconnected_twilio_binding_calls(
    db: AsyncSession,
    *,
    channel_config: ChannelConfig,
    channel_binding_id: str,
    reason: str,
) -> None:
    if channel_config.channel_type != "twilio_voice":
        return
    from packages.core.services.voice.call_sessions import (
        cancel_unconnected_call_sessions_for_binding,
    )

    await cancel_unconnected_call_sessions_for_binding(
        db,
        channel_config_id=channel_config.id,
        channel_binding_id=channel_binding_id,
        reason=reason,
    )


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
    from packages.core.services.workspace_creators import workspace_creator_users

    creators = await workspace_creator_users(db, workspaces)
    return {workspace_id: _user_summary(user) or {} for workspace_id, user in creators.items()}


def _workspace_blueprint_id(settings: Any) -> str:
    """Return a stored durable blueprint id without guessing from its slug."""
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        installed_blueprint_record,
    )
    record = installed_blueprint_record(settings)
    if record.get("source") == "raw_payload":
        return ""
    blueprint_id = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    if blueprint_id:
        return blueprint_id
    return ""


async def _blueprint_payloads_for(db: AsyncSession, workspaces, *, digests: bool = False) -> dict[str, tuple]:
    """Map each workspace to the blueprint payload it would upgrade toward.

    Every current blueprint lives in ``workspace_blueprints``; a client
    delivery's release lives on its delivery row. Workspaces resolve only
    their recorded durable id; a display slug is not provenance.

    One query for all of them: this feeds the workspace list.

    ``digests=True`` gives a ``workspace_blueprints`` release's
    ``BlueprintDigest`` in place of its payload: enough for the update badge.
    """
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        installed_blueprint_record,
    )
    from packages.core.blueprints.seed import (
        BlueprintRowReference,
        PLATFORM_BLUEPRINT_ID_PREFIX,
        blueprint_payload_digests,
        resolve_blueprint_rows,
    )

    from packages.core.models.client_delivery import DELIVERY_BLUEPRINT_ID_PREFIX

    from packages.core.blueprints.team_packages import is_team_package_ref, team_package_owner

    payloads: dict[str, tuple] = {}
    references: dict[str, BlueprintRowReference] = {}
    deliveries: dict[str, Any] = {}
    team_packages: dict[str, str] = {}

    for ws in workspaces:
        settings = ws.settings if isinstance(getattr(ws, "settings", None), dict) else {}
        record = installed_blueprint_record(settings)
        # A raw JSON install intentionally has no update source. A source-less
        # slug-only historical record is equally unbound: neither the slug nor
        # its installed content proves which release created it.
        if record.get("source") == "raw_payload":
            continue
        blueprint_id = str(record.get(BLUEPRINT_ID_KEY) or "").strip() or None
        if is_team_package_ref(blueprint_id):
            # A team's own package: its latest release, and only for that team.
            if team_package_owner(blueprint_id) == ws.entity_id:
                team_packages[ws.id] = blueprint_id
        elif blueprint_id and blueprint_id.startswith(DELIVERY_BLUEPRINT_ID_PREFIX):
            # A client delivery is a release source too: the provider's latest
            # published export of their own Workspace.
            deliveries[ws.id] = ws
        elif blueprint_id:
            blueprint_slug = (
                blueprint_id.removeprefix(PLATFORM_BLUEPRINT_ID_PREFIX) or None
                if blueprint_id.startswith(PLATFORM_BLUEPRINT_ID_PREFIX)
                else None
            )
            references[ws.id] = BlueprintRowReference(
                blueprint_id,
                blueprint_slug,
            )

    if deliveries:
        from packages.core.models.client_delivery import ClientWorkspaceDelivery
        from packages.core.services.client_delivery import content_version, delivery_blueprint_id

        # Bound to the Workspace it installed: a delivery id copied into
        # another Workspace's settings resolves to nothing.
        rows = (await db.scalars(select(ClientWorkspaceDelivery).where(
            ClientWorkspaceDelivery.target_workspace_id.in_(list(deliveries)),
            ClientWorkspaceDelivery.status.in_(("delivered", "needs_setup")),
        ))).all()
        for row in rows:
            ws = deliveries.get(row.target_workspace_id)
            record_id = installed_blueprint_record(ws.settings).get(BLUEPRINT_ID_KEY)
            if row.target_entity_id == ws.entity_id and record_id == delivery_blueprint_id(row.id):
                payloads[ws.id] = (row.payload, content_version(row), record_id)

    if references:
        try:
            rows = await resolve_blueprint_rows(db, references, load_payload=not digests)
            from packages.core.constants.blueprints import BlueprintStatus

            # Only a published row is an update release. Archived/draft
            # content is editable and must never leak through upgrade preview
            # or be applied to an installed Workspace.
            rows = {
                workspace_id: row for workspace_id, row in rows.items()
                if row.status == BlueprintStatus.PUBLISHED
            }
            row_digests = (
                await blueprint_payload_digests(db, {row.id for row in rows.values()}) if digests else {}
            )
            for workspace_id, row in rows.items():
                if digests:
                    if (release := row_digests.get(row.id)) is not None:
                        digest, content_version = release
                        payloads[workspace_id] = (digest, content_version, row.id)
                else:
                    payload = row.payload if isinstance(row.payload, dict) else None
                    if payload:
                        payloads[workspace_id] = (payload, row.content_version, row.id)
        except Exception:
            logger.warning("blueprint freshness: payload lookup failed", exc_info=True)

    if team_packages:
        from packages.core.blueprints.registry import latest_release

        for workspace_id, ref in team_packages.items():
            release = await latest_release(db, ref)
            if release is not None and isinstance(release.payload, dict):
                payloads[workspace_id] = (release.payload, release.version, ref)

    return await _with_release_ranges(db, workspaces, payloads)


class _ResolvedBlueprint(tuple):
    """``(payload, version, blueprint_id)``, unpacked as before, carrying the
    install's position in the package's release history for the summary."""

    release_range: dict[str, Any] = {}


async def _with_release_ranges(db: AsyncSession, workspaces, payloads: dict[str, tuple]) -> dict[str, tuple]:
    """Attach latest version, releases behind and their changelogs."""
    from packages.core.blueprints.freshness import installed_blueprint_record
    from packages.core.blueprints.registry import installed_release_version, release_ranges

    if not payloads:
        return payloads
    installs = {
        ws.id: (payloads[ws.id][2], installed_release_version(installed_blueprint_record(ws.settings)))
        for ws in workspaces
        if ws.id in payloads and payloads[ws.id][2]
    }
    try:
        ranges = await release_ranges(db, installs)
    except Exception:
        logger.warning("blueprint freshness: release history lookup failed", exc_info=True)
        return payloads
    out: dict[str, tuple] = {}
    for workspace_id, resolved in payloads.items():
        out[workspace_id] = _ResolvedBlueprint(resolved)
        out[workspace_id].release_range = ranges.get(workspace_id, {})
    return out


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
    summary.update(_release_range_fields(getattr(resolved, "release_range", None)))
    return summary


def _release_range_fields(release_range: dict[str, Any] | None) -> dict[str, Any]:
    """Where an install stands in its package's release history; null when
    the registry does not know the package or the installed version."""
    release_range = release_range or {}
    return {
        "latest_version": release_range.get("latest_version"),
        "versions_behind": release_range.get("versions_behind"),
        "changelog": list(release_range.get("changelog") or []),
    }


def _to_response(
    ws,
    *,
    creator: User | dict[str, Any] | None = None,
    blueprint_payload: tuple | None = None,
    can_manage: bool = False,
    can_delete: bool = False,
) -> WorkspaceResponse:
    from packages.core.blueprints.registry import settings_without_package_alias

    creator_summary = _coerce_user_summary(creator)
    return WorkspaceResponse(
        id=ws.id, entity_id=ws.entity_id, name=ws.name,
        artifact_folder_id=getattr(ws, "artifact_folder_id", None),
        description=ws.description, category=ws.category,
        address=ws.address, kind=ws.kind,
        operating_context=ws.operating_context,
        primary_work=ws.primary_work,
        operating_model=ws.operating_model or {},
        settings=settings_without_package_alias(ws.settings or {}), status=ws.status,
        blueprint_update=_blueprint_update_for(ws, blueprint_payload),
        created_at=ws.created_at, updated_at=getattr(ws, "updated_at", None),
        created_by_user_id=(creator_summary or {}).get("id"),
        created_by_name=(creator_summary or {}).get("name"),
        created_by_email=(creator_summary or {}).get("email"),
        created_by_avatar_url=(creator_summary or {}).get("avatar_url"),
        can_manage=can_manage,
        can_delete=can_delete,
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


async def _require_workspace(
    db: AsyncSession, workspace_id: str, entity_id: str,
    *, initialize_artifact_folder: bool = True,
):
    ws = await get_workspace(
        db, workspace_id, entity_id, initialize_artifact_folder=initialize_artifact_folder,
    )
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


async def _require_workspace_read(
    db: AsyncSession,
    workspace_id: str,
    user: User,
    *,
    credential: AuthenticatedUserCredential | None = None,
    initialize_artifact_folder: bool = True,
):
    ws = await _require_workspace(
        db, workspace_id, user.entity_id, initialize_artifact_folder=initialize_artifact_folder,
    )
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


async def _require_workspace_operation_manage(
    db: AsyncSession, workspace_id: str, user: User,
    *, patches: list[dict[str, Any]] | None = None,
):
    # Operation writes can add reusable references. Match their service and
    # purge lock order before management authorization locks the Workspace.
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle
    from packages.core.services.workspace_operation_service import operation_patches_require_lifecycle_lock

    try:
        needs_lifecycle_lock = patches is None or operation_patches_require_lifecycle_lock(patches)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if needs_lifecycle_lock:
        await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    return await _require_workspace_manage(db, workspace_id, user)


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

    try:
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
        errors = exc.validation.get("errors") or []
        if errors and all(error.get("code") == "no_changes" for error in errors):
            # These authenticated API writers ensure a target state; an
            # unchanged configuration is a valid retry, not an applied draft.
            # apply_operation_draft already locked and revalidated this
            # Workspace. Keep lifecycle/schedule reconciliation, but neither
            # retain the temporary draft nor invent a new operation revision.
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.entity_id != entity_id:
                raise HTTPException(404, "Workspace not found")
            await db.delete(draft)
            await sync_workspace_runtime_schedules(db, workspace, user_id=user_id)
            return {
                "status": "unchanged",
                "draft": None,
                "workspace_revision": int(workspace.operation_revision or 0),
                "tasks_created": [],
            }
        raise HTTPException(400, exc.validation) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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

# All a Workspace list reads from the two JSON columns that grow with the
# Workspace. A Blueprint install record alone can run to tens of kilobytes per
# Workspace; GET /workspaces/{id} still returns the full values.
_LIST_SUMMARY_SETTINGS_KEYS = ("access_mode", "sandbox")


def _workspace_list_summary_columns(settings: Any, operating_model: Any) -> tuple[dict, dict]:
    settings = settings if isinstance(settings, dict) else {}
    model = operating_model if isinstance(operating_model, dict) else {}
    app_view = model.get("app_view")
    open_as = app_view.get("open_as") if isinstance(app_view, dict) else None
    return (
        {key: settings[key] for key in _LIST_SUMMARY_SETTINGS_KEYS if key in settings},
        {"app_view": {"open_as": open_as}} if open_as else {},
    )


@router.get("", response_model=list[WorkspaceResponse])
async def list_my_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    initialize: bool = True,
    view: Literal["full", "summary"] = "full",
):
    from sqlalchemy import func
    from packages.core.models.task import Task
    from packages.core.models.goal import Goal
    from packages.core.models.workspace import AgentSubscription

    credential = AuthenticatedUserCredential.from_user(user)
    workspaces = await list_workspaces(db, user.entity_id, initialize_artifact_folder=initialize)
    authorized = await ResourcePermissionGate.authorize_workspace_batch_read(
        db,
        credential=credential,
        workspace_ids={str(workspace.id) for workspace in workspaces},
    )
    if authorized is None:
        raise HTTPException(404, "Workspace not found")
    # An external collaborator lists only the Workspaces their live grants cover.
    collaborator_ids = collaborator_scope(user)
    workspaces = [
        workspace
        for workspace in workspaces
        if str(workspace.id) in authorized.workspace_ids
        and (collaborator_ids is None or str(workspace.id) in collaborator_ids)
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
    # Sidebar recency belongs to the viewer's own chat activity, rather than
    # Workspace configuration edits or background Agent/Task updates.
    from packages.core.models.task import Conversation, Message

    last_chat_by_workspace = dict((await db.execute(
        select(Conversation.workspace_id, func.max(Message.created_at))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(
            Conversation.entity_id == user.entity_id,
            Conversation.workspace_id.in_(ws_ids),
            Conversation.scope.in_(("workspace_main", "workspace_thread")),
            Message.role == "user",
            Message.meta["author_user_id"].as_string() == user.id,
        )
        .group_by(Conversation.workspace_id)
    )).all()) if ws_ids else {}

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

        # Every count of what needs the person comes from one rule, the one
        # the Workspace chat and its badge use.
        from packages.core.models.execution import ExecutionPlan, ExecutionStep
        from packages.core.workspace_chat.attention import workspace_attention
        from packages.core.workspace_chat.pending_actions import pending_action_total

        try:
            async with db.begin_nested():
                counts = await workspace_attention(db, user=user, workspace_ids=ws_ids)
            for wid, buckets in counts.items():
                stats_map[wid].update({key: value for key, value in buckets.items() if key != "unseen_action_keys"})
                stats_map[wid]["pending_actions"] = pending_action_total(buckets)
        except Exception:
            # Degrading to 0 hides every badge in the sidebar, so say why.
            logger.warning("workspace pending-action stats failed", exc_info=True)

        # Tasks parked on the customer, whether or not they raised a card.
        hitl_task_ids: dict[str, set[str]] = {}
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
            select(ExecutionPlan.workspace_id, ExecutionPlan.task_id)
            .select_from(ExecutionStep)
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
            hitl_task_ids.setdefault(r.workspace_id, set()).add(r.task_id)
        for wid, task_ids in hitl_task_ids.items():
            stats_map[wid]["hitl_tasks"] = len(task_ids)

    creator_map = await _workspace_creator_summaries(db, workspaces)
    blueprint_payloads = await _blueprint_payloads_for(db, workspaces, digests=True)
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
    deletable_workspace_ids = await _deletable_workspace_ids(
        db, user, manageable_ids=manageable_workspace_ids,
    )
    result = []
    for ws in workspaces:
        resp = _to_response(
            ws,
            creator=creator_map.get(ws.id),
            blueprint_payload=blueprint_payloads.get(ws.id),
            can_manage=ws.id in manageable_workspace_ids,
            can_delete=ws.id in deletable_workspace_ids,
        )
        if view == "summary":
            resp.settings, resp.operating_model = _workspace_list_summary_columns(
                resp.settings, resp.operating_model,
            )
        data = resp.model_dump() if hasattr(resp, "model_dump") else resp.__dict__.copy()
        data["stats"] = stats_map.get(ws.id, {})
        data["last_chat_at"] = last_chat_by_workspace.get(ws.id)
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
        await sync_workspace_runtime_schedules(db, ws, user_id=user.id)
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
    return _to_response(
        ws,
        creator=user,
        can_manage=True,
        can_delete=ws.id in await _deletable_workspace_ids(db, user, manageable_ids={ws.id}),
    )


# ── Trash / Restore ─────────────────────────────────────────────────────────

@router.get("/trash/list", response_model=list[WorkspaceResponse])
async def list_trash(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspaces in the soft-delete grace window. Returns rows with
    ``deleted_at`` set; consumers can compute days-until-purge from
    that timestamp + ``WORKSPACE_PURGE_GRACE_DAYS``."""
    credential = AuthenticatedUserCredential.from_user(user)
    rows = await list_trashed_workspaces(db, user.entity_id)
    restorable_ids = await _restorable_workspace_ids(db, rows, credential=credential)
    rows = [ws for ws in rows if ws.id in restorable_ids]
    creator_map = await _workspace_creator_summaries(db, rows)
    payloads = await _blueprint_payloads_for(db, rows, digests=True)
    # Metadata reads can wait; filter again using current actor and ownership.
    restorable_ids = await _restorable_workspace_ids(db, rows, credential=credential)
    deletable_ids = await _deletable_workspace_ids(db, user, manageable_ids=restorable_ids)
    return [
        _to_response(
            ws,
            creator=creator_map.get(ws.id),
            blueprint_payload=payloads.get(ws.id),
            can_manage=True,
            can_delete=ws.id in deletable_ids,
        )
        for ws in rows
        if ws.id in restorable_ids
    ]


@router.get("/trash/grace-days")
async def get_grace_days():
    """Surface the configured grace window so the UI can render
    accurate "X days until permanent deletion" copy."""
    return {"grace_days": WORKSPACE_PURGE_GRACE_DAYS}


class WorkspaceTrashPurgeRequest(BaseModel):
    workspace_ids: list[str] = Field(min_length=1, max_length=100)


class WorkspaceTrashPurgeFailure(BaseModel):
    workspace_id: str
    reason: str


class WorkspaceTrashPurgeResponse(BaseModel):
    purged: list[str] = Field(default_factory=list)
    failed: list[WorkspaceTrashPurgeFailure] = Field(default_factory=list)


async def _permanently_delete_trashed_workspace(
    db: AsyncSession,
    user: User,
    workspace_id: str,
    *,
    credential: AuthenticatedUserCredential,
) -> None:
    """Hard-delete one Workspace from the trash now, as the nightly purge would.

    Raises ``HTTPException`` with the refusal reason when the Workspace is not
    in the caller's trash, the caller may not delete it, or provider work is
    still pending. Callers own the transaction and must commit or roll back.
    """
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    # Same lock order as restore: lifecycle, Workspace boundary, recipient.
    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await lock_workspace_access_boundary(
        db, workspace_id=workspace_id, entity_id=user.entity_id,
    )
    if not ws or ws.deleted_at is None:
        raise HTTPException(404, "Workspace not found in trash")
    await lock_workspace_recipient_authorization(
        db, workspace_id=workspace_id, entity_id=user.entity_id, user_id=user.id,
    )
    if workspace_id not in await _restorable_workspace_ids(db, [ws], credential=credential):
        raise HTTPException(404, "Workspace not found in trash")
    if workspace_id not in await _deletable_workspace_ids(db, user, manageable_ids={workspace_id}):
        raise HTTPException(
            403, "Only a workspace owner or an authorized organization owner/admin can delete this workspace",
        )
    if not await purge_workspace(db, workspace_id, expected_deleted_at=ws.deleted_at):
        raise HTTPException(
            409, "This workspace still has provider work in progress; try again after it settles",
        )


async def _finish_trash_purge(db: AsyncSession, user: User, workspace_ids: list[str]) -> None:
    """Run the same post-purge cleanup as the nightly sweep for these Workspaces."""
    from packages.core.services.plan_gate import invalidate_gate_cache
    from packages.core.services.workspace_artifact_purge import drain_all_workspace_artifact_purge_jobs

    invalidate_gate_cache(user.entity_id)
    for workspace_id in workspace_ids:
        _invalidate_workspace_context(workspace_id)
    await drain_all_workspace_artifact_purge_jobs(db, entity_id=user.entity_id)


@router.delete("/trash/{workspace_id}", status_code=204)
async def permanently_delete_trashed_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete one Workspace from the trash before its grace window ends.

    Independently stored and reused Knowledge documents are retained,
    matching the nightly purge."""
    credential = AuthenticatedUserCredential.from_user(user)
    try:
        await _permanently_delete_trashed_workspace(
            db, user, workspace_id, credential=credential,
        )
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    await _finish_trash_purge(db, user, [workspace_id])


@router.post("/trash/purge", response_model=WorkspaceTrashPurgeResponse)
async def permanently_delete_trashed_workspaces(
    req: WorkspaceTrashPurgeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete several trashed Workspaces, one transaction each.

    A refused Workspace is reported in ``failed`` and does not stop the rest."""
    credential = AuthenticatedUserCredential.from_user(user)
    purged: list[str] = []
    failed: list[WorkspaceTrashPurgeFailure] = []
    for workspace_id in dict.fromkeys(req.workspace_ids):
        try:
            await _permanently_delete_trashed_workspace(
                db, user, workspace_id, credential=credential,
            )
        except HTTPException as exc:
            # Rollback expires the signed-in user; reload it before the next item.
            await db.rollback()
            await db.refresh(user)
            failed.append(WorkspaceTrashPurgeFailure(
                workspace_id=workspace_id, reason=str(exc.detail),
            ))
            continue
        await db.commit()
        purged.append(workspace_id)
    if purged:
        await _finish_trash_purge(db, user, purged)
    return WorkspaceTrashPurgeResponse(purged=purged, failed=failed)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    initialize: bool = True,
):
    credential = AuthenticatedUserCredential.from_user(user)
    ws = await _require_workspace_read(
        db, workspace_id, user, credential=credential, initialize_artifact_folder=initialize,
    )
    creator_map = await _workspace_creator_summaries(db, [ws])
    blueprint_payload = (await _blueprint_payloads_for(db, [ws], digests=True)).get(ws.id)
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
        can_delete=can_manage and ws.id in await _deletable_workspace_ids(db, user, manageable_ids={ws.id}),
    )


async def _app_view_workspace(db: AsyncSession, workspace: Workspace, user: User, draft_id: str | None):
    """The Workspace an App read shows: with ``draft_id``, the page that open
    design draft stages, for the people who may approve it."""
    if not draft_id:
        return workspace
    from packages.core.services.workspace_app_view import app_view_draft_preview

    if not await user_can_manage_workspace(db, workspace_id=workspace.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "Only people who manage this Workspace can preview its page changes")
    preview = await app_view_draft_preview(db, workspace, draft_id)
    if preview is None:
        raise HTTPException(404, "App page draft not found")
    return preview


@router.get("/{workspace_id}/app-view")
async def get_workspace_app_view(
    workspace_id: str,
    include_catalog: bool = True,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    initialize: bool = True,
    draft_id: str | None = Query(default=None, max_length=26),
):
    workspace = await _app_view_workspace(db, await _require_workspace_read(
        db, workspace_id, user, initialize_artifact_folder=initialize,
    ), user, draft_id)
    from packages.core.services.workspace_app_view import app_cover, load_workspace_app_view
    from packages.core.services.workspace_access import user_can_write_workspace_artifacts
    from packages.core.models.blueprint import WorkspaceBlueprint

    can_start = await user_can_write_workspace_artifacts(
        db, workspace_id=workspace_id, user_id=user.id, entity_role=user.role,
    )
    from packages.core.i18n import get_locale

    view = await load_workspace_app_view(
        db, workspace, user=user, include_catalog=include_catalog, locale=get_locale(),
    )
    blueprint = (workspace.settings or {}).get("_blueprint") or {}
    blueprint_id = blueprint.get("blueprint_id") if isinstance(blueprint, dict) else None
    starter_cover_url = None
    if (not workspace.cover_image_url and not view["config"].get("cover_image_url")
            and isinstance(blueprint_id, str) and blueprint_id):
        starter_cover_url = (await db.execute(select(WorkspaceBlueprint.cover_image_url).where(
            WorkspaceBlueprint.id == blueprint_id,
            or_(WorkspaceBlueprint.entity_id == user.entity_id, WorkspaceBlueprint.entity_id.is_(None)),
        ))).scalar_one_or_none()
    cover_image_url, cover_source = app_cover(workspace.cover_image_url, view["config"], starter_cover_url)
    return {
        **view,
        "cover_image_url": cover_image_url,
        "cover_source": cover_source,
        "can_start": can_start and not draft_id,
    }


@router.get("/{workspace_id}/app-view/components/{component_key}/contexts")
async def get_workspace_app_component_contexts(
    workspace_id: str,
    component_key: str,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    draft_id: str | None = Query(default=None, max_length=26),
    request: Request = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List only the contexts declared by one App data component.

    This is a read surface: workspace readers retain access when execution is
    paused or setup is incomplete, independently from ``can_start``.
    """
    workspace = await _app_view_workspace(db, await _require_workspace_read(db, workspace_id, user), user, draft_id)
    from packages.core.services.media_urls import media_url_scope, signer_for_request
    from packages.core.services.workspace_app_data import AppDataError, component_contexts
    try:
        from packages.core.i18n import get_locale

        # Browser sessions get signed cover thumbnails; other callers' payload is unchanged.
        with media_url_scope(signer_for_request(request, user)):
            return await component_contexts(db, workspace, user=user, component_key=component_key,
                                            cursor=cursor, limit=limit, locale=get_locale())
    except AppDataError as exc:
        raise HTTPException(404 if exc.code == "component_not_found" else 422, exc.payload()) from exc


@router.get("/{workspace_id}/app-view/components/{component_key}/data")
async def get_workspace_app_component_data(
    workspace_id: str,
    component_key: str,
    context_kind: str = Query(..., pattern="^(project|launch_run|workspace)$"),
    context_id: str = Query(..., min_length=1, max_length=128),
    block: str | None = Query(default=None, min_length=1, max_length=64),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    window: str | None = Query(default=None, max_length=64),
    draft_id: str | None = Query(default=None, max_length=26),
    filters: str | None = Query(default=None, max_length=1000, description="JSON object: a list block's filter index -> chosen value"),
    q: str | None = Query(default=None, max_length=100),
    parent: str | None = Query(default=None, min_length=1, max_length=26, description="A tree block: the record whose projects to read"),
    level: int | None = Query(default=None, ge=1, le=2, description="A tree block: 1 for a record's projects, 2 for theirs"),
    request: Request = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve one configured App component without accepting a query DSL."""
    chosen: dict[str, str] | None = None
    if filters:
        try:
            parsed = json.loads(filters)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict) or len(parsed) > 3 or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items()):
            raise HTTPException(422, {"code": "invalid_filters", "message": "filters is an object of up to three chosen values."})
        chosen = parsed
    workspace = await _app_view_workspace(db, await _require_workspace_read(db, workspace_id, user), user, draft_id)
    from packages.core.services.media_urls import media_url_scope, signer_for_request
    from packages.core.services.workspace_app_data import AppDataError, component_data
    try:
        from packages.core.i18n import get_locale

        # Browser sessions get signed media thumbnails; other callers' payload is unchanged.
        with media_url_scope(signer_for_request(request, user)):
            return await component_data(db, workspace, user=user, component_key=component_key,
                                        context_kind=context_kind, context_id=context_id,
                                        block_key=block, cursor=cursor, limit=limit, window=window,
                                        locale=get_locale(), filters=chosen, query=q,
                                        tree_parent=parent, tree_level=level)
    except AppDataError as exc:
        raise HTTPException(404 if exc.code in {"component_not_found", "context_not_found"} else 422,
                            exc.payload()) from exc


@router.get("/{workspace_id}/app-view/sections/{section_id}")
async def get_workspace_app_section(
    workspace_id: str,
    section_id: str,
    draft_id: str | None = Query(default=None, max_length=26),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One App section's data; the section's source comes from the saved page."""
    workspace = await _app_view_workspace(db, await _require_workspace_read(db, workspace_id, user), user, draft_id)
    from packages.core.services.workspace_app_sections import AppSectionNotFound, load_workspace_app_section

    try:
        from packages.core.i18n import get_locale

        return await load_workspace_app_section(db, workspace, user=user, section_id=section_id, locale=get_locale())
    except AppSectionNotFound:
        raise HTTPException(404, "App section not found")


@router.get(
    "/{workspace_id}/setup-status",
    response_model=WorkspaceSetupStatusResponse,
)
async def get_workspace_setup_status(
    workspace_id: str,
    workflow_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the live Blueprint setup gate, not its install-time snapshot.

    With ``workflow_id``, ``ready`` says whether that Workflow may start: setup
    it never reaches (a connection only other Workflows use) does not hold it up.
    The incomplete checks are the Workspace's either way.
    """

    workspace = await _require_workspace_read(db, workspace_id, user)
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
        evaluate_workspace_automatic_setup_status,
    )

    # What this person can start depends on their own connections (their own paired Chrome).
    status = await evaluate_workspace_blocking_setup(db, workspace, actor_user_id=user.id)
    if status is None:
        return WorkspaceSetupStatusResponse(
            ready=True,
            status="not_required",
            summary="No blocking Blueprint setup is required.",
        )
    incomplete_checks = list(status.details.get("incomplete_checks") or [])
    automatic_state, automatic_task_id = await evaluate_workspace_automatic_setup_status(
        db, workspace, incomplete_checks,
    )
    ready = not status.blocks_work
    if not ready and workflow_id:
        from packages.core.models.workflow import WorkflowDefinition
        from packages.core.services.workspace_readiness import blocking_setup_blocks_workflow

        definition = await db.get(WorkflowDefinition, workflow_id)
        steps = definition.steps if definition is not None and definition.entity_id == workspace.entity_id else None
        ready = not blocking_setup_blocks_workflow(status, steps)
    return WorkspaceSetupStatusResponse(
        ready=ready,
        status=status.status,
        summary=status.summary,
        incomplete_checks=incomplete_checks,
        automatic_setup_state=automatic_state,
        automatic_setup_task_id=automatic_task_id,
    )


@router.post("/{workspace_id}/setup-input")
async def submit_workspace_setup_input(
    workspace_id: str,
    req: WorkspaceSetupInputRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Record an owner's explicit answer to a declared materials setup check."""
    workspace = await _require_workspace_manage(db, workspace_id, user)
    check_key = req.check_key.strip()
    answer = req.text.strip()
    if not check_key or not answer:
        raise HTTPException(400, "A setup check and answer are required")

    setup = (workspace.settings or {}).get("blocking_setup") or {}
    checks = setup.get("checks") if isinstance(setup, dict) else []
    check = next((item for item in checks if isinstance(item, dict)
                  and item.get("key") == check_key), None)
    if not check or check.get("kind") != "workspace_user_materials":
        raise HTTPException(404, "Materials setup check not found")

    from packages.core.models.base import generate_ulid
    from packages.core.humans.service import open_commitment, resolve_commitment
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
        workspace_materials_check_accepts_text,
    )

    if not workspace_materials_check_accepts_text(check):
        raise HTTPException(400, "This setup check accepts files only")

    status = await evaluate_workspace_blocking_setup(db, workspace)
    if status is None or not any(
        item.get("key") == check_key
        for item in status.details.get("incomplete_checks") or []
    ):
        return {"accepted": False}

    commitment = await open_commitment(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        request_kind="input",
        source_kind="workspace_setup",
        source_id=generate_ulid(),
        expected_input=str(check.get("question") or ""),
        participant_id=user.id,
    )
    await resolve_commitment(
        db,
        commitment,
        status="fulfilled",
        response={"text": answer, "setup_check_key": check_key},
        participant_id=user.id,
    )
    await db.commit()
    return {"accepted": True}


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
    await _require_workspace_operation_manage(db, workspace_id, user)
    # A collaborator's settings change is limited by update_workspace
    # (collaborator_scope.COLLABORATOR_WRITABLE_SETTINGS).
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
        blueprint_payload=(await _blueprint_payloads_for(db, [ws], digests=True)).get(ws.id),
        can_manage=True,
        can_delete=ws.id in await _deletable_workspace_ids(db, user, manageable_ids={ws.id}),
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
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    # Match lifecycle lock order, then recheck current target and actor scope.
    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await lock_workspace_access_boundary(
        db, workspace_id=workspace_id, entity_id=user.entity_id,
    )
    if not ws or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await lock_workspace_recipient_authorization(
        db, workspace_id=workspace_id, entity_id=user.entity_id, user_id=user.id,
    )
    await _require_workspace_read(db, workspace_id, user)
    await _require_workspace_manage(db, workspace_id, user)
    if workspace_id not in await _deletable_workspace_ids(db, user, manageable_ids={workspace_id}):
        raise HTTPException(
            403, "Only a workspace owner or an authorized organization owner/admin can delete this workspace",
        )
    try:
        ok = await soft_delete_workspace(db, workspace_id, user.entity_id)
    except ScheduledAgentLineageDeletionBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Workspace not found")
    from packages.core.services.plan_gate import invalidate_gate_cache

    await db.commit()
    invalidate_gate_cache(user.entity_id)
    _invalidate_workspace_context(workspace_id)


@router.post("/{workspace_id}/restore", response_model=WorkspaceRestoreResponse)
async def restore_one_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recover a soft-deleted workspace before the grace window
    expires. After hard-purge there's nothing to restore."""
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    credential = AuthenticatedUserCredential.from_user(user)
    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await lock_workspace_access_boundary(
        db, workspace_id=workspace_id, entity_id=user.entity_id,
    )
    if not ws or ws.deleted_at is None:
        raise HTTPException(404, "Workspace not found in trash")
    await lock_workspace_recipient_authorization(
        db, workspace_id=workspace_id, entity_id=user.entity_id, user_id=user.id,
    )
    if workspace_id not in await _restorable_workspace_ids(db, [ws], credential=credential):
        raise HTTPException(404, "Workspace not found in trash")
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
    response = _to_response(
        ws,
        creator=creator_map.get(ws.id),
        blueprint_payload=(await _blueprint_payloads_for(db, [ws], digests=True)).get(ws.id),
        can_manage=True,
        can_delete=ws.id in await _deletable_workspace_ids(db, user, manageable_ids={ws.id}),
    )
    return WorkspaceRestoreResponse(
        **response.model_dump(),
        unmapped_agents=list(getattr(ws, "_restore_unmapped_agents", ())),
    )


# ── Pause / Resume ──────────────────────────────────────────────────────────

_WORKSPACE_LIFECYCLE_BODIES = {
    ("start", "completed"):
        "Now working on its own · it reviews, plans and prepares the next tasks.",
    ("start_without_goals", "completed"):
        "Now working on its own · it reviews, plans and prepares the next tasks.",
    ("pause", "completed"):
        "Set to manual · it only does what you ask; workflow schedules keep running.",
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
    """Pause the Workspace Strategist without disabling deployed Workflows."""
    ws = await _require_workspace_operation_manage(db, workspace_id, user)
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
        # Re-assert the Strategist lifecycle invariant when a request is
        # retried, without touching independently configured automations.
        from packages.core.services.scheduler_service import (
            pause_workspace_autonomy_jobs,
        )

        await pause_workspace_autonomy_jobs(db, workspace_id, user.entity_id)
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
    ws = await _require_workspace_operation_manage(db, workspace_id, user)
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
        await sync_workspace_runtime_schedules(db, ws, user_id=user.id)
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
    await _require_workspace_operation_manage(db, workspace_id, user)
    await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_operating_model_update",
        patches=[{"op": "operating_model.replace", "payload": {"operating_model": operating_model}}],
    )
    ws = await _require_workspace_operation_manage(db, workspace_id, user)
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
    await _require_workspace_operation_manage(db, workspace_id, user, patches=req.patches or [])
    from packages.core.services.workspace_operation_service import (
        create_operation_draft,
        draft_to_dict,
    )

    try:
        draft = await create_operation_draft(
            db,
            workspace_id,
            user.entity_id,
            user_id=user.id,
            source_event_id=req.source_event_id,
            initial_patches=req.patches,
            allowed_patch_operations=req.allowed_patch_operations,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    await _require_workspace_operation_manage(db, workspace_id, user, patches=req.patches)
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
            allowed_patch_operations=req.allowed_patch_operations,
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
        refresh_operation_draft_review,
    )

    draft = await refresh_operation_draft_review(db, draft_id, user.entity_id, workspace_id)
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    validation = draft.validation
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
        refresh_operation_draft_review,
    )

    draft = await refresh_operation_draft_review(db, draft_id, user.entity_id, workspace_id)
    if draft is None:
        raise HTTPException(404, "Operation draft not found")
    diff = draft.diff
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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
            expected_review_fingerprint=req.expected_review_fingerprint,
            allowed_patch_operations=req.allowed_patch_operations,
        )
    except OperationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OperationValidationError as exc:
        raise HTTPException(400, exc.validation) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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


@router.post("/{workspace_id}/app-view/design")
async def design_workspace_app_view_endpoint(
    workspace_id: str,
    req: AppViewDesignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Command the App design sub-agent; its page is staged as an app_view.update draft."""
    await _require_workspace_operation_manage(db, workspace_id, user, patches=[{"op": "app_view.update", "payload": {}}])
    from packages.core.services.workspace_app_designer import design_workspace_app_view

    result = await design_workspace_app_view(
        db, workspace_id=workspace_id, entity_id=user.entity_id, user_id=user.id,
        brief=req.brief, feedback=req.feedback, replaces_draft_id=req.draft_id, audience=req.audience,
    )
    if not result.get("ok"):
        await db.rollback()
        raise HTTPException(422, {"error": result.get("error"), "design_notes": result.get("design_notes")})
    await db.commit()
    return {key: result[key] for key in ("draft", "candidate", "recipes", "warnings", "design_notes", "self_check")}


@router.get("/{workspace_id}/app-view/test")
async def test_workspace_app_view_endpoint(
    workspace_id: str,
    draft_id: str | None = Query(default=None, max_length=26),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The App page's self-check, saved or as an open draft stages it: read-only, and it proves reads, not clicks."""
    workspace = await _require_workspace_read(db, workspace_id, user, initialize_artifact_folder=False)
    if not await user_can_manage_workspace(db, workspace_id=workspace.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "Only people who manage this Workspace can test its page")
    from packages.core.services.workspace_app_selfcheck import self_check_app_page

    return await self_check_app_page(db, await _app_view_workspace(db, workspace, user, draft_id), user=user)


@router.get("/{workspace_id}/app-view/design/audience")
async def workspace_app_view_audience_endpoint(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Who the App page could be for: purpose, people, roles, what waits on them and usage."""
    workspace = await _require_workspace_read(db, workspace_id, user)
    if not await user_can_manage_workspace(db, workspace_id=workspace.id, user_id=user.id, entity_role=user.role):
        raise HTTPException(403, "Only people who manage this Workspace can design its page")
    from packages.core.services.workspace_app_designer import workspace_app_audience

    audience = await workspace_app_audience(db, workspace_id=workspace_id, entity_id=user.entity_id)
    if audience is None:
        raise HTTPException(404, "Workspace not found")
    return audience


@router.post("/{workspace_id}/operation/repair")
async def repair_workspace_operation_runtime_endpoint(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_workspace_operation_manage(db, workspace_id, user)
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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


async def _require_collaborator_workspace_agent(
    db: AsyncSession, user: User, workspace_id: str, agent_id: str | None,
) -> None:
    """An external collaborator may only use an Agent that already belongs to this Workspace.

    Otherwise an agent id from anywhere in the customer company could be pulled
    into the Workspace, and with it that Agent's prompt, tools and knowledge.
    """
    await require_collaborator_references(
        db, user_id=user.id, entity_id=user.entity_id, workspace_id=workspace_id,
        refs={"agent": {agent_id}}, user=user,
    )


@router.post("/{workspace_id}/agents")
async def map_agent(
    workspace_id: str,
    req: AgentMappingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await _require_workspace_operation_manage(db, workspace_id, user)
    await _require_collaborator_workspace_agent(db, user, workspace_id, req.agent_id)
    from packages.core.models.workspace import AgentSubscription

    deployed_service = (await db.execute(select(AgentSubscription.service_key).where(
        AgentSubscription.entity_id == user.entity_id,
        AgentSubscription.workspace_id == workspace_id,
        AgentSubscription.agent_id == req.agent_id,
        AgentSubscription.status == "active",
    ).limit(1))).scalar_one_or_none()
    if deployed_service is not None and deployed_service != req.service_key:
        raise HTTPException(409, "This agent already serves another service in this workspace. Choose a different agent.")
    patches = []
    if req.new_service is not None:
        service = req.new_service
        if not service.name.strip() or not service.key.strip() or service.key != service.key.strip() or service.key != req.service_key:
            raise HTTPException(400, "The new service requires a name and matching service key")
        existing_subscription = (await db.execute(select(AgentSubscription.id).where(
            AgentSubscription.entity_id == user.entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == service.key,
            AgentSubscription.status == "active",
        ).limit(1))).scalar_one_or_none()
        if any(
            str(row.get("key") or row.get("service_key") or "").strip() == service.key
            for row in ((workspace.operating_model or {}).get("services") or [])
            if isinstance(row, dict)
        ) or existing_subscription is not None:
            raise HTTPException(409, "This service already exists. Select the existing service or choose a different name.")
        patches.append({"op": "service_role.upsert", "payload": service.model_dump()})
    patches.append({
        "op": "agent_mapping.upsert",
        "payload": {
            "mapping": {
                "service_key": req.service_key,
                "agent_id": req.agent_id,
                "custom_prompt": req.custom_prompt,
            },
        },
    })
    result = await _apply_workspace_operation_patches(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
        source_event_id="api_agent_map",
        patches=patches,
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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
        select(Integration.provider, Integration.owner_user_id).where(
            Integration.entity_id == user.entity_id,
            Integration.status == "active",
        )
    )).all()
    if collaborator_scope(user) is not None:
        # A collaborator sees only the connections usable in this Workspace:
        # their own, and providers a member bound to it.
        from packages.core.services.collaborator_scope import customer_bound_providers

        bound = await customer_bound_providers(db, entity_id=user.entity_id, workspace_id=workspace_id)
        integration_rows = [
            row for row in integration_rows
            if row.owner_user_id == user.id or canonical_provider_key(row.provider) in bound
        ]
    for provider, _owner in integration_rows:
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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
    await _require_workspace_operation_manage(db, workspace_id, user)
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


# ── Attention ────────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/attention-items")
async def list_workspace_attention_items(
    workspace_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Open situations that may need a person: most urgent first, then oldest."""
    from packages.core.attention.query import list_open_attention, serialize_attention_item

    await _require_workspace_read(db, workspace_id, user)
    items = await list_open_attention(
        db, entity_id=user.entity_id, workspace_id=workspace_id, limit=limit,
    )
    return {"items": [serialize_attention_item(item) for item in items], "count": len(items)}


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
    from packages.core.models.workspace_collaboration import WorkspaceCollaboration
    from packages.core.services.external_collaboration_access import WORKSPACE_ACCESS_ROLES

    await _require_workspace_read(db, workspace_id, user)
    result = await db.execute(
        select(WorkspaceStaff, Staff, UserModel, WorkspaceCollaboration.role, WorkspaceCollaboration.delivery_id)
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
            WorkspaceCollaboration,
            and_(
                WorkspaceCollaboration.id == WorkspaceStaff.collaboration_id,
                WorkspaceCollaboration.entity_id == user.entity_id,
                WorkspaceCollaboration.workspace_id == WorkspaceStaff.workspace_id,
                WorkspaceCollaboration.user_id == WorkspaceStaff.user_id,
                WorkspaceCollaboration.role.in_(WORKSPACE_ACCESS_ROLES),
                WorkspaceCollaboration.status == "active",
            ),
        )
        .outerjoin(
            UserModel,
            and_(
                UserModel.id == WorkspaceStaff.user_id,
                # A grant-derived seat belongs to someone from another company:
                # their User row's entity is wherever they last switched to.
                or_(UserModel.entity_id == user.entity_id, WorkspaceCollaboration.id.is_not(None)),
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
            avatar_url=(
                user_record.avatar_url
                if user_record and user_record.avatar_url
                else staff.avatar_url if staff else None
            ),
            role=membership.role,
            added_by=getattr(membership, "added_by", None),
            added_at=getattr(membership, "added_at", None),
            expires_at=getattr(membership, "expires_at", None),
            status=getattr(membership, "status", None),
            created_at=membership.created_at,
            collaboration_role=collaboration_role,
            collaboration_team=bool(collaboration_role and delivery_id),
        )
        for membership, staff, user_record, collaboration_role, delivery_id in rows
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
        # The Staff link alone is not an authority change (see set_link_columns).
        await set_link_columns(db, ws_staff, staff_id=req.staff_id)
        ws_staff.user_id = linked_user_id
        ws_staff.role = req.role
        # A customer assignment takes over any collaboration-derived seat.
        ws_staff.collaboration_id = None
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
        display_name=staff.name,
        email=staff.email,
        avatar_url=staff.avatar_url,
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
    """Remove a staff member from a workspace.

    ``staff_id`` names the assignment by its Staff row or, for a seat that
    belongs to a user directly (no Staff row), by the seat id or the user id.
    """
    await _require_workspace_manage(db, workspace_id, user)
    target = (await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            or_(
                WorkspaceStaff.staff_id == staff_id,
                and_(
                    WorkspaceStaff.staff_id.is_(None),
                    or_(WorkspaceStaff.id == staff_id, WorkspaceStaff.user_id == staff_id),
                ),
            ),
        ).order_by(WorkspaceStaff.id).limit(1)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Staff assignment not found")
    if target.collaboration_id is not None:
        # A seat derived from an external collaboration follows its grant.
        raise HTTPException(409, "Change the collaboration to remove this person from the Workspace")
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
    ws = await _require_workspace_operation_manage(db, workspace_id, user)
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
    """Pause the Strategist runtime for a workspace."""
    ws = await _require_workspace_operation_manage(db, workspace_id, user)
    if ws.status == "active":
        ws.status = "paused"
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
    return {
        "workspace_id": ws.id,
        "heartbeat_enabled": False,
        "status": ws.status,
    }


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
    connection_metadata = await _workspace_channel_connection_metadata(
        db,
        list(rows),
        entity_id=user.entity_id,
    )

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
            **connection_metadata.get(ch.id, {}),
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
    connection_metadata = await _workspace_channel_connection_metadata(
        db,
        list(rows),
        entity_id=user.entity_id,
    )
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
            **connection_metadata.get(ch.id, {}),
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
    await _require_collaborator_workspace_agent(db, user, workspace_id, resolved_agent_id)

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
        cfg = {key: value for key, value in request_config.items() if key != "resident_pms"}
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
            owner_user_id=user.id,
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
    if "resident_pms" in request_config:
        from packages.core.services import webchat_resident
        try:
            request_config["resident_pms"] = await webchat_resident.publish_target(
                db, request_config["resident_pms"], user=user, cc=channel_config, binding=None,
            )
        except webchat_resident.ResidentAccessError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc
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
        if (
            existing.agent_id != resolved_agent_id
            or existing.agent_subscription_id != (sub.id if sub else None)
            or existing.workspace_id != workspace_id
            or existing.status != "active"
        ):
            await _cancel_unconnected_twilio_binding_calls(
                db,
                channel_config=channel_config,
                channel_binding_id=existing.id,
                reason=(
                    "Twilio Voice Agent binding changed before the call connected."
                ),
            )
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
    await _require_collaborator_workspace_agent(db, user, workspace_id, resolved_agent_id)
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
    if "resident_pms" in req_config:
        from packages.core.services import webchat_resident
        try:
            req_config["resident_pms"] = await webchat_resident.publish_target(
                db, req_config["resident_pms"], user=user, cc=channel_config, binding=binding,
            )
        except webchat_resident.ResidentAccessError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc
    if req_config:
        binding_config.update(req_config)

    if req.name is not None:
        binding.name = req.name.strip() or binding.name
    if routing_requested:
        if (
            binding.agent_id != resolved_agent_id
            or binding.agent_subscription_id != (sub.id if sub else None)
        ):
            await _cancel_unconnected_twilio_binding_calls(
                db,
                channel_config=channel_config,
                channel_binding_id=binding.id,
                reason=(
                    "Twilio Voice Agent binding changed before the call connected."
                ),
            )
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
    if cc_id:
        channel_config = await db.scalar(
            select(ChannelConfig).where(
                ChannelConfig.id == cc_id,
                ChannelConfig.entity_id == user.entity_id,
            )
        )
        if channel_config is not None:
            await _cancel_unconnected_twilio_binding_calls(
                db,
                channel_config=channel_config,
                channel_binding_id=binding.id,
                reason=(
                    "Twilio Voice Agent binding was removed before the call connected."
                ),
            )
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


async def _find_default_workspace_collection(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> DocumentGroup | None:
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
    return None


async def _ensure_default_workspace_collection(
    db: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> DocumentGroup:
    """Ensure the default RAG collection while holding the Workspace row lock."""
    existing = await _find_default_workspace_collection(
        db, workspace_id=workspace_id, entity_id=entity_id,
    )
    if existing is not None:
        return existing

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


def _set_workspace_knowledge_policy(ws: Workspace, knowledge: dict[str, Any]) -> bool:
    """Write under the Workspace row lock and invalidate older operation drafts."""
    operating_model = dict(ws.operating_model or {})
    if knowledge == dict(operating_model.get("knowledge") or {}):
        return False
    operating_model["knowledge"] = knowledge
    ws.operating_model = operating_model
    ws.operation_revision = int(ws.operation_revision or 0) + 1
    return True


def _remove_group_from_operating_model(ws: Workspace, group_id: str) -> bool:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    default_ids = list(knowledge.get("default_group_ids") or [])
    if group_id in default_ids:
        knowledge["default_group_ids"] = [gid for gid in default_ids if gid != group_id]
    purposes = dict(knowledge.get("group_purposes") or {})
    if group_id in purposes:
        purposes.pop(group_id)
        knowledge["group_purposes"] = purposes
    return _set_workspace_knowledge_policy(ws, knowledge)


def _ensure_group_default_in_operating_model(ws: Workspace, group_id: str) -> bool:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    default_ids = list(knowledge.get("default_group_ids") or [])
    if group_id in default_ids:
        return False
    knowledge["default_group_ids"] = [group_id, *default_ids]
    return _set_workspace_knowledge_policy(ws, knowledge)


def _set_group_purpose_in_operating_model(ws: Workspace, group_id: str, purpose: str) -> bool:
    operating_model = dict(ws.operating_model or {})
    knowledge = dict(operating_model.get("knowledge") or {})
    purposes = dict(knowledge.get("group_purposes") or {})
    if purpose:
        purposes[group_id] = purpose
    elif group_id in purposes:
        purposes.pop(group_id)
    else:
        return False
    knowledge["group_purposes"] = purposes
    return _set_workspace_knowledge_policy(ws, knowledge)


def _workspace_group_source_folder_id(group: DocumentGroup) -> str | None:
    source = (group.settings or {}).get("knowledge_folder_source")
    if not isinstance(source, dict):
        return None
    folder_id = str(source.get("folder_id") or "").strip()
    return folder_id or None


async def _workspace_document_folder_snapshot(
    db: AsyncSession,
    *,
    entity_id: str,
    documents: list[Document],
    root_folder_ids: set[str],
) -> dict[str, DocumentFolder]:
    """Load document ancestors and every descendant of a projected root."""
    pending = {
        str(document.folder_id)
        for document in documents
        if getattr(document, "folder_id", None)
    }
    pending.update(root_folder_ids)
    folder_by_id: dict[str, DocumentFolder] = {}
    while pending:
        batch = set(sorted(pending)[:500])
        pending.difference_update(batch)
        rows = list((await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == entity_id,
                DocumentFolder.id.in_(batch),
            )
        )).scalars().all())
        for folder in rows:
            folder_by_id[str(folder.id)] = folder
            if folder.parent_id and str(folder.parent_id) not in folder_by_id:
                pending.add(str(folder.parent_id))

    frontier = set(root_folder_ids)
    visited: set[str] = set()
    while frontier:
        batch = set(sorted(frontier - visited)[:500])
        if not batch:
            break
        visited.update(batch)
        rows = list((await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == entity_id,
                DocumentFolder.parent_id.in_(batch),
            )
        )).scalars().all())
        for folder in rows:
            folder_id = str(folder.id)
            folder_by_id[folder_id] = folder
            if folder_id not in visited:
                frontier.add(folder_id)
        frontier.difference_update(batch)
    return folder_by_id


async def _workspace_visible_folder_ids(
    db: AsyncSession,
    *,
    access_ctx: DocumentAccessContext,
    folder_by_id: dict[str, DocumentFolder],
) -> set[str]:
    """Return the readable, user-facing folders from a loaded snapshot."""
    from packages.core.services.knowledge_visibility import is_user_visible_folder_path

    def folder_path(folder: DocumentFolder) -> str:
        parts: list[str] = []
        current = folder
        seen: set[str] = set()
        while current is not None and str(current.id) not in seen:
            seen.add(str(current.id))
            parts.append(str(current.name))
            current = (
                folder_by_id.get(str(current.parent_id))
                if current.parent_id
                else None
            )
        return "/".join(reversed(parts))

    visible_folder_ids: set[str] = set()
    for folder_id in sorted(folder_by_id):
        folder = folder_by_id[folder_id]
        if not is_user_visible_folder_path(folder_path(folder)):
            continue
        if await access_ctx.folder_path_readable(db, folder_id):
            visible_folder_ids.add(folder_id)
    return visible_folder_ids


def _workspace_group_folder_projection(
    documents: list[Document],
    *,
    folder_by_id: dict[str, DocumentFolder],
    visible_folder_ids: set[str],
    root_folder_id: str | None,
    display_names: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Return this group's visible folder tree, rebased below its display root."""
    included_folder_ids: set[str] = set()
    if root_folder_id:
        for candidate_id in folder_by_id:
            current_id: str | None = candidate_id
            candidate_path: list[str] = []
            seen: set[str] = set()
            while current_id and current_id != root_folder_id and current_id not in seen:
                seen.add(current_id)
                folder = folder_by_id.get(current_id)
                if folder is None or current_id not in visible_folder_ids:
                    break
                candidate_path.append(current_id)
                current_id = str(folder.parent_id) if folder.parent_id else None
            if current_id == root_folder_id:
                included_folder_ids.update(candidate_path)
    else:
        for document in documents:
            current_id = str(getattr(document, "folder_id", None) or "") or None
            seen: set[str] = set()
            while current_id and current_id not in seen:
                seen.add(current_id)
                folder = folder_by_id.get(current_id)
                if folder is None or current_id not in visible_folder_ids:
                    break
                included_folder_ids.add(current_id)
                current_id = str(folder.parent_id) if folder.parent_id else None

    folders = []
    for folder_id in sorted(
        included_folder_ids,
        key=lambda value: (str(folder_by_id[value].name).casefold(), value),
    ):
        folder = folder_by_id[folder_id]
        parent_id = str(folder.parent_id) if folder.parent_id else None
        if parent_id == root_folder_id or parent_id not in included_folder_ids:
            parent_id = None
        folders.append({
            "id": folder_id,
            "name": folder.name,
            **({"display_name": display_names[folder_id]} if display_names and folder_id in display_names else {}),
            "parent_id": parent_id,
        })
    return folders, included_folder_ids


def _workspace_document_payload(document: Document, folder_ids: set[str]) -> dict[str, Any]:
    folder_id = str(getattr(document, "folder_id", None) or "") or None
    return {
        "id": document.id,
        "name": document.name,
        "file_type": document.file_type,
        "mime_type": document.mime_type,
        "file_size": document.file_size,
        "vector_status": document.vector_status,
        "folder_id": folder_id if folder_id in folder_ids else None,
    }


@router.get("/{workspace_id}/documents")
async def list_workspace_documents(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    initialize: bool = True,
):
    """List Workspace Knowledge; initialize=false returns a read-only snapshot."""
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroupMember

    ws = await _require_workspace_read(
        db, workspace_id, user, initialize_artifact_folder=initialize,
    )
    default_group = await _find_default_workspace_collection(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    default_ids = ((ws.operating_model or {}).get("knowledge") or {}).get("default_group_ids") or []
    if initialize and (default_group is None or default_group.id not in default_ids):
        # Only lazy initialization needs a write lock. Refresh after waiting,
        # then recheck both access and collection existence before mutation.
        ws = await lock_workspace_access_boundary(
            db, workspace_id=workspace_id, entity_id=user.entity_id,
        )
        if ws is None or ws.deleted_at is not None:
            raise HTTPException(404, "Workspace not found")
        await _require_workspace_read(db, workspace_id, user)
        default_group = await _ensure_default_workspace_collection(
            db, workspace_id=workspace_id, entity_id=user.entity_id,
        )
        changed_default_ids = _ensure_group_default_in_operating_model(ws, default_group.id)
        # Release before the potentially large document/permission projection,
        # including when another request completed initialization first.
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

    # Load membership once, then authorize each distinct document once even
    # when it belongs to several Knowledge Nets or is also a generated artifact.
    members = []
    if groups:
        members = (await db.execute(
            select(DocumentGroupMember.group_id, Document)
            .join(Document, DocumentGroupMember.document_id == Document.id)
            .where(
                DocumentGroupMember.group_id.in_([group.id for group in groups]),
                Document.entity_id == user.entity_id,
            )
        )).all()

    artifact_candidates, _artifact_total = await list_documents(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        limit=None,
    )
    document_by_id = {document.id: document for _group_id, document in members}
    document_by_id.update({document.id: document for document in artifact_candidates})
    root_folder_ids = {
        folder_id
        for folder_id in (
            str(ws.artifact_folder_id or "").strip() or None,
            *(_workspace_group_source_folder_id(group) for group in groups),
        )
        if folder_id
    }
    folder_by_id = await _workspace_document_folder_snapshot(
        db,
        entity_id=user.entity_id,
        documents=list(document_by_id.values()),
        root_folder_ids=root_folder_ids,
    )
    access_ctx = await DocumentAccessContext.load(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        document_ids=set(document_by_id),
        folder_ids=set(folder_by_id),
    )
    await access_ctx.preload_documents(db, list(document_by_id.values()))
    readable_documents = {
        document.id: document
        for document in document_by_id.values()
        if workspace_id in access_ctx.document_workspace_ids(document)
        and await access_ctx.can_read_document(db, document, workspace_id=workspace_id)
    }
    documents_by_group: dict[str, list[Document]] = {group.id: [] for group in groups}
    grouped_doc_ids: set[str] = set()
    for group_id, document in members:
        if document.id in readable_documents:
            documents_by_group[group_id].append(document)
            grouped_doc_ids.add(document.id)
    group_rows = [(group, documents_by_group[group.id]) for group in groups]
    artifact_docs = [
        document for document in artifact_candidates
        if document.id in readable_documents and document.id not in grouped_doc_ids
    ]
    visible_folder_ids = await _workspace_visible_folder_ids(
        db,
        access_ctx=access_ctx,
        folder_by_id=folder_by_id,
    )
    from packages.core.services.workspace_artifacts import workspace_archive_folder_labels

    # Task/Workflow run archives are filed under their IDs; show them readably.
    archive_labels = await workspace_archive_folder_labels(
        db,
        entity_id=user.entity_id,
        folders=[
            (folder.id, folder.name, folder.parent_id)
            for folder in folder_by_id.values()
            if folder.id in visible_folder_ids
        ],
    )

    out = []
    for dg, documents in group_rows:
        folders, group_folder_ids = _workspace_group_folder_projection(
            documents,
            folder_by_id=folder_by_id,
            visible_folder_ids=visible_folder_ids,
            root_folder_id=_workspace_group_source_folder_id(dg),
            display_names=archive_labels,
        )
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
            "document_count": len(documents),
            "folders": folders,
            "documents": [
                _workspace_document_payload(document, group_folder_ids)
                for document in documents
            ],
        })
    artifact_root_id = str(ws.artifact_folder_id or "") or None
    artifact_folders, artifact_folder_ids = _workspace_group_folder_projection(
        artifact_docs,
        folder_by_id=folder_by_id,
        visible_folder_ids=visible_folder_ids,
        root_folder_id=artifact_root_id,
        display_names=archive_labels,
    )
    if artifact_docs or artifact_folders:
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
            "folders": artifact_folders,
            "documents": [
                _workspace_document_payload(document, artifact_folder_ids)
                for document in artifact_docs
            ],
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
        or await effective_user_has_permission(
            db, user, Permission.DOCS_MANAGE_ALL
        )
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
        or await effective_user_has_permission(
            db, user, Permission.DOCS_MANAGE_ALL
        )
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
    seed_kwargs: dict = {"entity_id": user.entity_id, "user_id": user.id, "kind": req.kind}
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

class CreditUsageSummaryResponse(BaseModel):
    today_credits: int
    week_credits: int
    all_time_credits: int
    timezone: str
    # A client delivery trial's provider pays this Workspace's AI usage.
    paid_by_provider: bool = False


@router.get(
    "/{workspace_id}/credit-usage-summary",
    response_model=CreditUsageSummaryResponse,
)
async def get_workspace_credit_usage_summary_endpoint(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await _require_workspace_read(db, workspace_id, user)
    from packages.core.budget import get_workspace_credit_usage_summary

    summary = await get_workspace_credit_usage_summary(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        workspace_created_at=workspace.created_at,
        timezone_name=user.timezone,
        owner_entity_id=workspace.entity_id,
    )
    return CreditUsageSummaryResponse(**summary.__dict__)


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
    await _require_workspace_operation_manage(db, workspace_id, user)
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
    #: A listed release of the Workspace's own Blueprint to move to, newer or
    #: older; omitted = latest.
    target_version: str | None = Field(None, min_length=1, max_length=64)


class BlueprintUpgradeApplyRequest(BlueprintUpgradeVariableValuesRequest):
    expected_blueprint_fingerprint: str = Field(min_length=1, max_length=128)
    conflict_resolutions: list[BlueprintConflictResolutionRequest] = Field(
        default_factory=list,
        max_length=200,
    )


BLUEPRINT_UPGRADE_PROTOCOL_VERSION = 2


class BlueprintConfigurationResolutionRequest(BaseModel):
    key: str = Field(min_length=1, max_length=512)
    resolution: Literal["keep_yours", "use_blueprint"]


class BlueprintReconfigurationApplyRequest(BlueprintUpgradeVariableValuesRequest):
    target_version: str | None = Field(None, min_length=1, max_length=64)
    expected_blueprint_fingerprint: str = Field(min_length=1, max_length=128)
    expected_plan_fingerprint: str = Field(min_length=1, max_length=128)
    configuration_resolutions: list[BlueprintConfigurationResolutionRequest] = Field(
        default_factory=list, max_length=2000,
    )


async def _blueprint_release_for(db: AsyncSession, ws, target_version: str | None) -> tuple | None:
    """The payload this Workspace would move to: its Blueprint's latest, or
    the listed release ``target_version`` names (newer or older)."""
    resolved = await _blueprint_payload_for(db, ws)
    if resolved is None or not target_version:
        return resolved
    from packages.core.blueprints.registry import listed_release

    release = await listed_release(db, resolved[2], target_version)
    if release is None:
        raise HTTPException(404, f"Version {target_version} of this Blueprint is not available.")
    chosen = _ResolvedBlueprint((release.payload, release.version, resolved[2]))
    chosen.release_range = getattr(resolved, "release_range", {})
    return chosen


async def _release_choice_fields(db: AsyncSession, ws, package_ref: str | None, target_version: str | None) -> dict[str, Any]:
    """The releases an install can move to, and what the step to this one is."""
    from packages.core.blueprints.freshness import installed_blueprint_record
    from packages.core.blueprints.registry import (
        installed_release_version, listed_release, package_releases, version_sort_key,
    )
    from packages.core.blueprints.release_diff import diff_releases

    releases = await package_releases(db, package_ref) if package_ref else None
    if not releases:
        return {"available_versions": [], "target_version": target_version, "downgrade": False, "version_change": None}
    target = target_version or releases["tags"].get("latest")
    installed = installed_release_version(installed_blueprint_record(ws.settings))
    change = None
    if installed and target and installed != target:
        before = await listed_release(db, package_ref, installed)
        after = await listed_release(db, package_ref, target)
        if before is not None and after is not None:
            change = {**diff_releases(before.payload, after.payload), "from": installed, "to": target}
    return {
        "available_versions": releases["versions"],
        "target_version": target,
        "downgrade": bool(installed and target and version_sort_key(target) < version_sort_key(installed)),
        "version_change": change,
    }


async def _reconfiguration_payload(db, *, workspace, variable_values, target_version=None):
    from packages.core.blueprints.installer import resolve_install_variables
    from packages.core.blueprints.reconfigure import used_variable_payload

    resolved = await _blueprint_release_for(db, workspace, target_version)
    if resolved is None or not isinstance(resolved[0], dict):
        raise HTTPException(409, "The source Blueprint is no longer available.")
    source, version, source_id = resolved
    usable = used_variable_payload(source)
    values, _keys = _blueprint_upgrade_variable_inputs(
        workspace=workspace, payload=usable, supplied=variable_values,
    )
    effective_values = {
        item["key"]: values.get(item["key"], item.get("default"))
        for item in usable.get("contract", {}).get("variables", [])
        if item["key"] in values or "default" in item
    }
    payload, personalization = resolve_install_variables(usable, values)
    return source, payload, usable, personalization, effective_values, version, source_id


async def _preview_blueprint_reconfiguration(*, workspace_id, variable_values, channel_config_ids, user, db,
                                             target_version=None):
    from apps.api.routers.blueprints import _payload_setup_preview
    from packages.core.blueprints.installer import InstallError
    from packages.core.blueprints.reconfigure import plan, public_plan, used_variable_payload, retained_runtime_payload
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await _require_workspace_manage(db, workspace_id, user)
    try:
        source, payload, usable, _personalization, values, version, source_id = await _reconfiguration_payload(
            db, workspace=ws, variable_values=variable_values, target_version=target_version,
        )
    except InstallError:
        resolved = await _blueprint_release_for(db, ws, target_version)
        source = resolved[0] if resolved else None
        usable = used_variable_payload(source) if source else None
        values, _keys = _blueprint_upgrade_variable_inputs(
            workspace=ws, payload=usable, supplied=variable_values,
        )
        effective_values = {
            item["key"]: values.get(item["key"], item.get("default"))
            for item in (usable or {}).get("contract", {}).get("variables", [])
            if item["key"] in values or "default" in item
        }
        from packages.core.blueprints.reconfigure import _state, active_runs

        state = await _state(db, ws)
        point = state.restore_point if state else None
        return {
            "upgrade_protocol_version": 3, "variables_ready": False,
            "setup_preview": _payload_setup_preview(usable).model_dump(),
            "setup_preflight": {"ready": True, "blocking_count": 0, "requirements": []},
            "items": [], "active_runs": await active_runs(db, workspace=ws),
            "baseline_known": bool(state and state.baseline),
            "resolved_variable_values": effective_values, "can_revert": bool(point),
            "restore_point": {"applied_at": point.get("applied_at") or point.get("upgraded_at"),
                              "blueprint_version": point.get("to_version")} if point else None,
            "blueprint_fingerprint": "", "plan_fingerprint": "",
            **_release_range_fields(getattr(resolved, "release_range", None)),
            **(await _release_choice_fields(db, ws, resolved[2] if resolved else None, target_version)),
        }
    try:
        result = await plan(db, workspace=ws, payload=payload, source_payload=source,
                            current_version=version, source_blueprint_id=source_id,
                            channel_config_ids=channel_config_ids, actor=user)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        **public_plan(result), "variables_ready": True,
        "resolved_variable_values": values, "resolved_variable_keys": list(values),
        "setup_preview": _payload_setup_preview(usable).model_dump(),
        "setup_preflight": await _blueprint_upgrade_setup_preflight(
            db, payload=payload, user=user, workspace_id=ws.id,
            selected_channel_config_ids=channel_config_ids,
        ),
        "kept_setup_preflight": await _blueprint_upgrade_setup_preflight(
            db, payload=payload,
            retained_contract=(await retained_runtime_payload(db, workspace=ws, payload=payload))["contract"],
            user=user, workspace_id=ws.id, selected_channel_config_ids={},
        ),
        **(await _release_range_for(db, ws, source_id)),
        **(await _release_choice_fields(db, ws, source_id, target_version)),
    }


@router.get("/{workspace_id}/blueprint/upgrade/v3")
async def preview_blueprint_reconfiguration(
    workspace_id: str, target_version: str | None = Query(None, min_length=1, max_length=64),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    return await _preview_blueprint_reconfiguration(
        workspace_id=workspace_id, variable_values={}, channel_config_ids={}, user=user, db=db,
        target_version=target_version,
    )


@router.post("/{workspace_id}/blueprint/upgrade/v3/preview")
async def preview_blueprint_reconfiguration_with_variables(
    workspace_id: str, req: BlueprintUpgradePreviewRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    return await _preview_blueprint_reconfiguration(
        workspace_id=workspace_id, variable_values=req.variable_values,
        channel_config_ids=req.channel_config_ids, user=user, db=db, target_version=req.target_version,
    )


@router.post("/{workspace_id}/blueprint/upgrade/v3")
async def apply_blueprint_reconfiguration(
    workspace_id: str, req: BlueprintReconfigurationApplyRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    from packages.core.blueprints.reconfigure import apply, retained_runtime_payload, RUNTIME_KEY
    from packages.core.blueprints.installer import InstallError
    from packages.core.blueprints.upgrade import BlueprintUpgradeAccessDeniedError
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await _require_workspace_manage(db, workspace_id, user)
    try:
        source, payload, _usable, personalization, _values, version, source_id = await _reconfiguration_payload(
            db, workspace=ws, variable_values=req.variable_values, target_version=req.target_version,
        )
        runtime_choice = next((item.resolution for item in req.configuration_resolutions if item.key == RUNTIME_KEY), None)
        # The preview can omit an unchanged, previously customized contract.
        retained = await retained_runtime_payload(db, workspace=ws, payload=payload)
        from packages.core.blueprints.reconfigure import plan, runtime_contract
        intended = await plan(db, workspace=ws, payload=payload, source_payload=source,
                             current_version=version, source_blueprint_id=source_id,
                             channel_config_ids=req.channel_config_ids, actor=user)
        runtime_item = next(item for item in intended["items"] if item["key"] == RUNTIME_KEY)
        keep_runtime = runtime_choice == "keep_yours" or (
            runtime_item["action"] == "unchanged" and runtime_item["current_content"] != runtime_contract(payload)
        )
        preflight = await _blueprint_upgrade_setup_preflight(
            db, payload=payload, user=user, workspace_id=ws.id,
            retained_contract=retained["contract"] if keep_runtime else None,
            selected_channel_config_ids={} if keep_runtime else req.channel_config_ids,
        )
        if not preflight["ready"]:
            raise HTTPException(409, {"code": "blueprint_setup_required", "preflight": preflight,
                                      "message": "Complete the reviewed Blueprint setup before upgrading."})
        result = await apply(
            db, workspace=ws, payload=payload, source_payload=source, actor=user, by_user_id=user.id,
            current_version=version, source_blueprint_id=source_id, personalization=personalization,
            expected_blueprint_fingerprint=req.expected_blueprint_fingerprint,
            expected_plan_fingerprint=req.expected_plan_fingerprint,
            configuration_resolutions=[item.model_dump() for item in req.configuration_resolutions],
            channel_config_ids=req.channel_config_ids,
            resolved_channel_config_ids={item["requirement_key"]: item["resource_id"]
                for item in preflight["requirements"] if item.get("requirement_key") and item.get("resource_id")
            } | req.channel_config_ids,
        )
    except BlueprintUpgradeAccessDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, InstallError) as exc:
        raise HTTPException(409, str(exc)) from exc
    from packages.core.services.workspace_update_offer import UPDATE_CHOICE, close_update_cards

    # However it was applied, an offered release is no longer waiting.
    await close_update_cards(db, entity_id=ws.entity_id, workspace_id=ws.id, user_id=user.id, resolution={
        "choice": UPDATE_CHOICE, "payload": {
            "version": version, "updated": len(result.get("updated") or []),
            "kept": len(result.get("kept_yours") or []), "can_revert": bool(result.get("can_revert")),
        },
    })
    await db.commit()
    return result


class BlueprintRecommendedUpgradeRequest(BaseModel):
    target_version: str | None = Field(None, min_length=1, max_length=64)


@router.post("/{workspace_id}/blueprint/upgrade/v3/recommended")
async def apply_recommended_blueprint_upgrade(
    workspace_id: str, req: BlueprintRecommendedUpgradeRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Update in one step with the upgrade dialog's default choices: the
    author's changes are taken and the Workspace's own edits are kept.

    Anything that needs a person (runs still open, setup or Blueprint
    variables to fill in) is a 409 ``upgrade_needs_review`` naming it, and the
    caller opens the upgrade dialog instead.
    """
    from packages.core.services.workspace_update_offer import default_resolutions

    preview = await _preview_blueprint_reconfiguration(
        workspace_id=workspace_id, variable_values={}, channel_config_ids={}, user=user, db=db,
        target_version=req.target_version,
    )
    items = preview.get("items") or []
    resolutions = default_resolutions(items)
    runtime = next((item for item in items if item["key"] == "runtime_requirements"), None)
    keeps_runtime = runtime is not None and (
        runtime["current_content"] != runtime["new_content"] if runtime["action"] == "unchanged"
        else next(r["resolution"] for r in resolutions if r["key"] == runtime["key"]) == "keep_yours"
    )
    preflight = (preview.get("kept_setup_preflight") if keeps_runtime else None) or preview.get("setup_preflight") or {}
    reason = (
        "active_runs" if preview.get("active_runs")
        else "variables" if not preview.get("variables_ready")
        else "setup" if not preflight.get("ready")
        else None
    )
    if reason:
        raise HTTPException(409, {
            "code": "upgrade_needs_review", "reason": reason,
            "active_runs": preview.get("active_runs") or [],
            "message": "This update needs a review before it can be applied.",
        })
    result = await apply_blueprint_reconfiguration(workspace_id, BlueprintReconfigurationApplyRequest(
        target_version=req.target_version,
        expected_blueprint_fingerprint=preview["blueprint_fingerprint"],
        expected_plan_fingerprint=preview["plan_fingerprint"],
        configuration_resolutions=resolutions,
    ), user, db)
    return {**result, "version": preview.get("current_version")}


async def _release_range_for(db: AsyncSession, ws, package_ref: str | None) -> dict[str, Any]:
    from packages.core.blueprints.freshness import installed_blueprint_record
    from packages.core.blueprints.registry import installed_release_version, release_ranges

    if not package_ref:
        return _release_range_fields(None)
    record = installed_blueprint_record(ws.settings)
    ranges = await release_ranges(db, {ws.id: (package_ref, installed_release_version(record))})
    return _release_range_fields(ranges.get(ws.id))


async def _blueprint_payload_for(db: AsyncSession, ws) -> tuple | None:
    """The payload this workspace would upgrade toward — built-in,
    marketplace or client delivery, resolved the same way the list does."""
    return (await _blueprint_payloads_for(db, [ws])).get(ws.id)


async def _blueprint_upgrade_setup_preflight(
    db: AsyncSession,
    *,
    payload: dict | None,
    user: User,
    workspace_id: str,
    selected_channel_config_ids: dict[str, str] | None = None,
    retained_contract: dict | None = None,
) -> dict:
    if not payload:
        return {"ready": True, "blocking_count": 0, "requirements": []}
    from packages.core.blueprints.setup_preflight import (
        BlueprintSetupPreflightError,
        BlueprintSetupPreflightFactory,
    )
    from packages.core.blueprints.payload import PayloadError, validate_payload

    try:
        if retained_contract is not None:
            # Retained setup is a persisted contract, not a new Blueprint made
            # by combining old requirements with unaccepted upstream agents.
            validate_payload(payload)
            result = await BlueprintSetupPreflightFactory.from_contract(
                db,
                contract=retained_contract,
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=workspace_id,
                selected_channel_config_ids=selected_channel_config_ids,
            )
        else:
            result = await BlueprintSetupPreflightFactory.from_payload(
                db,
                payload=payload,
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=workspace_id,
                selected_channel_config_ids=selected_channel_config_ids,
            )
    except (BlueprintSetupPreflightError, PayloadError) as exc:
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
    if not isinstance(source_payload, dict):
        raise HTTPException(409, "The source Blueprint is no longer available.")
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
        **_release_range_fields(getattr(resolved, "release_range", None)),
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
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
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
    if not isinstance(source_payload, dict):
        raise HTTPException(409, "The source Blueprint is no longer available.")
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
    except InstallError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from packages.core.blueprints.reconfigure import invalidate_after_legacy_upgrade

    await invalidate_after_legacy_upgrade(db, workspace=ws)
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
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await _require_workspace_manage(db, workspace_id, user)
    from packages.core.blueprints.upgrade import (
        BlueprintUpgradeAccessDeniedError,
        BlueprintUpgradePlanChangedError,
        legacy_restore_point,
        revert,
    )

    from packages.core.blueprints.reconfigure import _state, revert as revert_configuration

    state = await _state(db, ws)
    use_v3 = state is not None and state.restore_point is not None and not legacy_restore_point(state)
    undone_version = (state.restore_point or {}).get("to_version") if use_v3 else None

    try:
        result = await (revert_configuration if use_v3 else revert)(
            db,
            workspace=ws,
            by_user_id=user.id,
            actor=user,
        )
    except BlueprintUpgradeAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except BlueprintUpgradePlanChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if undone_version:
        from packages.core.services.workspace_update_offer import mark_update_undone

        await mark_update_undone(db, entity_id=ws.entity_id, workspace_id=ws.id, version=undone_version)
    await db.commit()
    return result


@router.post("/{workspace_id}/blueprint/releases")
async def publish_workspace_package_release(
    workspace_id: str,
    file: UploadFile = File(...),
    release_notes: str | None = Form(None, max_length=4000),
    dry_run: bool = Form(False),
    adopt: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Make a Blueprint file — a ``.manor`` package or its JSON, built in any
    environment — the next release of this Workspace's own package.

    Nothing in the Workspace changes here: it then shows the update and takes
    it through the ordinary upgrade review (``/blueprint/upgrade/v3``). A
    Workspace installed before it had a package adopts one with ``adopt``:
    its matching components are linked and its current content becomes the
    merge base. ``dry_run`` reports the version, the changes and, for an
    adoption, which components were matched, without writing anything.
    """
    import json

    from packages.core.blueprints.freshness import installed_blueprint_record
    from packages.core.blueprints.package import MAX_PACKAGE_BYTES, PackageError, is_package, restore_assets, store_assets, unpack
    from packages.core.blueprints.payload import PayloadError, validate_payload
    from packages.core.blueprints.freshness import blueprint_content_fingerprint
    from packages.core.blueprints.registry import (
        BlueprintVersionConflictError, latest_release, next_release_version, release_for_fingerprint,
    )
    from packages.core.blueprints.release_diff import diff_releases
    from packages.core.blueprints.team_packages import (
        TeamPackageError, adopt_package, check_release_file, declared_package, describe_match, is_team_package_ref,
        match_components, package_name, package_scope, release_team_package, team_package_owner, team_package_ref,
    )
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    data = await file.read(MAX_PACKAGE_BYTES + 1)
    if len(data) > MAX_PACKAGE_BYTES:
        raise HTTPException(413, "The file is larger than a Blueprint package may be")
    try:
        payload, assets = unpack(data) if is_package(data) else (json.loads(data.decode("utf-8")), [])
    except (PackageError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"Not a Blueprint package or payload: {exc}") from exc

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await _require_workspace_manage(db, workspace_id, user)
    record = installed_blueprint_record(ws.settings)
    ref = str(record.get("blueprint_id") or "").strip()
    linked = is_team_package_ref(ref) and team_package_owner(ref) == ws.entity_id
    if not linked and ref and record.get("source") != "raw_payload":
        raise HTTPException(409, {
            "code": "blueprint_source_elsewhere",
            "message": "This Workspace updates from the Blueprint it was installed from; publish the new version there.",
        })
    if not linked and not adopt:
        raise HTTPException(409, {
            "code": "package_adoption_required",
            "message": "This Workspace has no package yet. Send the file with adopt to make it this Workspace's package.",
        })

    try:
        if linked:
            current = await latest_release(db, ref)
            name = ref.split(":", 2)[2]
            payload = check_release_file(current.payload if current else None, payload, name=name)
        else:
            validate_payload(payload)
    except (TeamPackageError, PayloadError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if release_notes:
        payload.setdefault("manifest", {})["release_notes"] = release_notes

    try:
        if not linked:
            name = package_name(payload, scope=await package_scope(db, payload, ws.entity_id))
            ref = team_package_ref(ws.entity_id, name)
            current = await latest_release(db, ref)
        if dry_run:
            changes = diff_releases(current.payload, payload) if current and current.payload else None
            existing = await release_for_fingerprint(db, ref, blueprint_content_fingerprint(payload))
            version = existing.version if existing else declared_package(payload).get("version") or (
                await next_release_version(
                    db, ref, payload, current_version=current.version,
                    stored_fingerprint=current.content_fingerprint,
                ) if current else "1.0.0"
            )
            result = {
                "dry_run": True, "package": {"ref": ref, "name": name},
                "current_version": current.version if current else None,
                "version": version, "would_publish": existing is None,
                "level": changes["min_bump"] if changes else None,
                "reasons": changes["reasons"] if changes else [],
                "components": changes["components"] if changes else None,
            }
            if not linked:
                result["adoption"] = describe_match(await match_components(db, workspace=ws, payload=payload))
            return result
        if assets:
            payload = restore_assets(payload, await store_assets(ws.entity_id, list(assets)))
        ref, release = await release_team_package(
            db, entity_id=ws.entity_id, payload=payload, package_ref=ref,
            changelog=release_notes or None, published_by=user.id,
        )
        adoption = None
        if not linked:
            adoption = await adopt_package(
                db, workspace=ws, payload=release.payload, package_ref=ref, release=release, by_user_id=user.id,
            )
    except (TeamPackageError, BlueprintVersionConflictError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.commit()
    return {
        "dry_run": False, "package": {"ref": ref, "name": name},
        "version": release.version,
        "published": current is None or current.version != release.version,
        "adoption": adoption,
    }
