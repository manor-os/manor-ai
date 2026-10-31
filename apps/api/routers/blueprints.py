"""Workspace Blueprint endpoints — export / list / install / promote.

Marketplace blueprints, including platform-owned built-ins, are served from
``workspace_blueprints``. Platform configs are published into that table by
the Cloud deployment publisher or the single-machine OSS startup owner;
historical ``builtin:<slug>`` and slug-only handles resolve to the same
canonical row.

Endpoints:

  POST   /api/v1/workspaces/{id}/export-blueprint-payload  export portable config
  POST   /api/v1/workspaces/{id}/export-blueprint   export current ws as draft
  GET    /api/v1/blueprints                          list mine
  GET    /api/v1/blueprints/{id}                     fetch one (mine or published)
  PUT    /api/v1/blueprints/{id}                     edit metadata
  DELETE /api/v1/blueprints/{id}                     delete
  POST   /api/v1/blueprints/{id}/install             install (mode=simulate|live)
  POST   /api/v1/workspaces/{id}/promote             Workspace simulation → live
  POST   /api/v1/workspaces/{id}/promote/preflight   read-only check
"""
from __future__ import annotations

import logging
import json
import os
import re
import secrets
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user, require_workspace_readable, require_workspace_writable
from packages.core.constants.blueprints import (
    BLUEPRINT_EDITABLE_STATUSES,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS,
    BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES,
    BLUEPRINT_KNOWLEDGE_LIST_PAGE_SIZE,
    BLUEPRINT_KNOWLEDGE_LIST_MAX_PAGE_SIZE,
    BlueprintStatus,
)
from packages.core.blueprints import (
    ExportError,
    InstallError,
    InstallMode,
    InstallResult,
    PromoteError,
    SimulationReport,
    export_workspace,
    get_solo_company_blueprint,
    install_blueprint,
    preflight_promote,
    promote_workspace,
    simulate_report,
)
from packages.core.blueprints.exporter import (
    ExportContext,
    list_exportable_knowledge_documents,
)
from packages.core.blueprints.installer import author_hard_blocks, resolve_install_variables
from packages.core.blueprints.payload import PayloadError, migrate_payload
from packages.core.blueprints.setup_preflight import (
    BlueprintSetupPreflight,
    BlueprintSetupPreflightError,
    BlueprintSetupPreflightFactory,
)
from packages.core.services.integration_resolution import is_user_facing_integration_provider
from packages.core.blueprints.seed import (
    platform_blueprint_id,
    resolve_blueprint_row,
)
from packages.core.governance.presets import list_presets
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import (
    BlueprintFavorite,
    WorkspaceBlueprint,
)
from packages.core.blueprints.registry import set_draft_payload, set_listing_manifest
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.permissions import Permission, check_effective_user_permission
from packages.core.models.client_delivery import DELIVERY_BLUEPRINT_ID_PREFIX
from packages.core.services.blueprint_cover_service import (
    build_blueprint_cover_template,
)
from packages.core.services.file_refs import parse_file_reference
from packages.core.services.generated_file_refs import entity_fs_open_url
from packages.core.services.entity_fs import (
    EntityFilesystemError,
    assert_entity_filesystem_ready,
    copy_entity_file_atomic,
    resolve_path,
)
from packages.core.services.entity_service import get_workspace
from packages.core.services.workspace_access import (
    lock_workspace_access_boundary,
    lock_workspace_recipient_authorization,
    user_can_manage_workspace,
)
from packages.core.services.marketplace_billing import (
    blueprint_delivery_is_paid,
    blueprint_delivery_source,
)
from packages.core.services.user_email_proof import email_is_verified

logger = logging.getLogger(__name__)

# Two routers because we need two prefixes (one workspace-scoped, one
# blueprint-scoped). Both registered in main.py.
blueprint_router = APIRouter(prefix="/api/v1/blueprints", tags=["blueprints"])
workspace_router = APIRouter(prefix="/api/v1/workspaces", tags=["blueprints"])

_BUILTIN_BLUEPRINT_PREFIX = "builtin:"
BLUEPRINT_SHOWCASE_MAX_ASSETS = 6
BLUEPRINT_SHOWCASE_IMAGE_MAX_BYTES = 12 * 1024 * 1024
BLUEPRINT_SHOWCASE_VIDEO_MAX_BYTES = 100 * 1024 * 1024
_SHOWCASE_IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_SHOWCASE_VIDEO_EXTENSIONS = {".m4v", ".mov", ".mp4", ".webm"}
_SHOWCASE_MIME_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}
_BLUEPRINT_CONFIGURATION_INVALID_DETAIL = (
    "Blueprint configuration or personalization is invalid."
)
_BLUEPRINT_PREFLIGHT_INVALID_DETAIL = (
    "Blueprint setup requirements could not be evaluated."
)


# ── Models ────────────────────────────────────────────────────────────

class BlueprintInstallVariableDeclaration(BaseModel):
    key: str = Field(
        ...,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,99}$",
    )
    label: str = Field(..., min_length=1, max_length=160)
    purpose: Optional[str] = Field(None, max_length=500)
    required: bool = False
    default: Optional[JsonValue] = None
    materialize: bool = True

    @field_validator("default")
    @classmethod
    def validate_default_size(cls, value: JsonValue | None) -> JsonValue | None:
        if value is None:
            return value
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 2000:
            raise ValueError("default must be at most 2000 UTF-8 bytes")
        return value


class ExportBlueprintPayloadRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    summary: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    usage_guide: Optional[str] = Field(None, max_length=10000)
    tags: list[str] = Field(default_factory=list)
    author_handle: Optional[str] = None
    author_display_name: Optional[str] = None
    # Personalization schema is authored by the Blueprint creator. Installer
    # values are supplied later and never become Marketplace credentials.
    install_variables: Optional[list[BlueprintInstallVariableDeclaration]] = Field(
        None,
        max_length=50,
    )
    # Section toggles — pass overrides only if you want to drop something
    include_subscriptions: bool = True
    include_goals: bool = True
    include_stats: bool = True
    include_scheduled_jobs: bool = True
    include_workflows: bool = True
    include_blocking_setup: bool = True
    include_custom_fields: bool = True
    include_governance: bool = True
    include_channel_requirements: bool = True
    include_session_requirements: bool = True
    include_embedded_agents: bool = True
    include_embedded_skills: bool = True
    include_knowledge_packs: bool = True
    knowledge_pack_mode: Optional[Literal["skeleton", "inline_text"]] = None
    include_starter_memory: bool = False
    # Deprecated alias retained for existing clients. True maps to
    # knowledge_pack_mode=inline_text when the explicit mode is omitted.
    include_memory_files: bool = False
    # Request-only local Document ids. They select exact safe text bodies
    # and are never persisted in the portable Blueprint payload.
    knowledge_document_ids: Optional[list[str]] = Field(
        None,
        max_length=BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS,
    )
    @model_validator(mode="after")
    def validate_knowledge_document_selection(self):
        if self.install_variables is not None:
            keys = [item.key for item in self.install_variables]
            if len(keys) != len(set(keys)):
                raise ValueError("install_variables keys must be unique")
        if self.knowledge_document_ids is None:
            return self
        normalized = [str(value).strip() for value in self.knowledge_document_ids]
        if any(not value for value in normalized):
            raise ValueError("knowledge_document_ids cannot contain empty values")
        self.knowledge_document_ids = list(dict.fromkeys(normalized))
        if not self.knowledge_document_ids:
            return self
        if not self.include_knowledge_packs:
            raise ValueError(
                "knowledge_document_ids requires include_knowledge_packs"
            )
        effective_mode = self.knowledge_pack_mode or (
            "inline_text" if self.include_memory_files else "skeleton"
        )
        if effective_mode != "inline_text":
            raise ValueError(
                "knowledge_document_ids requires knowledge_pack_mode='inline_text'"
            )
        return self


class WorkspaceBlueprintPayloadSource(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=26)
    export_options: ExportBlueprintPayloadRequest = Field(
        default_factory=ExportBlueprintPayloadRequest,
    )


class ExportBlueprintRequest(ExportBlueprintPayloadRequest):
    # Exact Marketplace counterpart when editing a previously published
    # Workspace. Omit for the first export; the server records the minted id.
    marketplace_blueprint_id: Optional[str] = Field(None, max_length=160)
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{1,118}[a-z0-9]$")
    title: str = Field(..., min_length=1, max_length=200)
    cover_image_url: Optional[str] = None
    # Re-freeze the Blueprint with this slug when it came from the same
    # Workspace. A draft/archived row is replaced in place; a published row
    # keeps serving its reviewed payload and holds the re-export as its next
    # version until that is reviewed (see pending_release.py).
    replace_existing: bool = False


class BlueprintExportKnowledgeGroup(BaseModel):
    id: str
    name: str


class BlueprintExportKnowledgeDocument(BaseModel):
    id: str
    name: str
    path: str
    file_size: int
    groups: list[BlueprintExportKnowledgeGroup] = Field(default_factory=list)
    groups_truncated: bool = False
    content_source: Literal["initial_template", "current_content"] = "current_content"
    required_by_workflows: list[str] = Field(default_factory=list)


class BlueprintInitialTemplateRequest(BaseModel):
    body_md: str | None = Field(..., max_length=BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES)

    @field_validator("body_md")
    @classmethod
    def validate_initial_body(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or len(value.encode("utf-8")) > BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES
        ):
            raise ValueError("Initial template must be nonempty text within the starter file size limit")
        return value


class BlueprintSetupItem(BaseModel):
    label: str
    key: Optional[str] = None
    kind: Optional[str] = None
    required: bool = False
    purpose: Optional[str] = None
    default: Any = None


class BlueprintSetupPreview(BaseModel):
    use_when: Optional[str] = None
    maturity_level: Optional[str] = None
    validation_summary: Optional[str] = None
    primary_work: Optional[str] = None
    runnable_in_simulation: bool = False
    blocking_todos_expected: Optional[int] = None
    required_variables: list[BlueprintSetupItem] = Field(default_factory=list)
    optional_variables: list[BlueprintSetupItem] = Field(default_factory=list)
    required_channels: list[BlueprintSetupItem] = Field(default_factory=list)
    optional_channels: list[BlueprintSetupItem] = Field(default_factory=list)
    required_sessions: list[BlueprintSetupItem] = Field(default_factory=list)
    optional_sessions: list[BlueprintSetupItem] = Field(default_factory=list)
    required_integrations: list[BlueprintSetupItem] = Field(default_factory=list)
    optional_integrations: list[BlueprintSetupItem] = Field(default_factory=list)
    first_week_outputs: list[str] = Field(default_factory=list)
    validation_evidence: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    not_included: list[str] = Field(default_factory=list)
    services: list[BlueprintSetupItem] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    #: never_allow entries the author shipped. Installed as approval gates
    #: unless the operator opts in with ``install_author_hard_blocks``.
    author_hard_blocks: list[str] = Field(default_factory=list)


class BlueprintShowcaseAsset(BaseModel):
    id: str
    kind: str
    url: str
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    uploaded_at: Optional[datetime] = None


class BlueprintCoverTemplateSpec(BaseModel):
    motif: Literal[
        "analytics",
        "commerce",
        "content",
        "distribution",
        "service",
        "video",
        "workspace",
    ]
    palette: Literal["blue", "peach", "sage", "stone"]
    variant: int = Field(ge=0, le=2)
    seed: int = Field(ge=0)




class BlueprintSummary(BaseModel):
    id: str
    slug: str
    title: str
    summary: Optional[str] = None
    tags: list[str]
    status: str
    install_count: int
    payload_version: str
    content_version: str
    source_workspace_id: Optional[str] = None
    cover_image_url: Optional[str] = None
    showcase_assets: list[BlueprintShowcaseAsset] = Field(default_factory=list)
    cover_template: BlueprintCoverTemplateSpec
    author_handle: Optional[str] = None
    author_display_name: Optional[str] = None
    author_avatar_url: Optional[str] = None
    remixed_from_id: Optional[str] = None
    favorite_count: int = 0
    is_favorited: bool = False
    remix_count: int = 0
    setup_preview: BlueprintSetupPreview = Field(default_factory=BlueprintSetupPreview)
    created_at: datetime
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    price_cents: Optional[int] = None
    list_price_cents: Optional[int] = None
    currency: str = "usd"
    purchase_count: int = 0
    has_share_token: bool = False
    share_allowed_emails: list[str] = Field(default_factory=list)
    # Whether the *caller* owns this row. Built-in marketplace blueprints
    # and other tenants' published blueprints are never "owned" by the
    # viewer, even though they're visible. The frontend uses this (not
    # id-prefix heuristics) to gate edit/delete/pricing/share controls.
    # Fail-closed default: every constructor sets it explicitly, so an
    # omission surfaces as missing owner controls, never leaked ones.
    is_owner: bool = False


class BlueprintDetail(BlueprintSummary):
    description: Optional[str] = None
    payload: dict[str, Any]
    purchased: bool = False
    # Set only by the detail route, in cloud deployments; always False in OSS.
    can_notify_installers: bool = False


class UpdateBlueprintRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    summary: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    usage_guide: Optional[str] = Field(None, max_length=10000)
    tags: Optional[list[str]] = None
    cover_image_url: Optional[str] = None


class BlueprintFavoriteResponse(BaseModel):
    is_favorited: bool
    favorite_count: int




class ShareTokenResponse(BaseModel):
    share_token: str


class ShareTokenRequest(BaseModel):
    # None retains the current setting when rotating an existing link.
    # An explicit empty list restores the legacy any-signed-in-user link.
    allowed_emails: Optional[list[str]] = Field(None, max_length=50)

    @field_validator("allowed_emails")
    @classmethod
    def normalize_emails(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        emails = [email.strip().casefold() for email in value]
        if any(
            len(email) > 255
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            for email in emails
        ):
            raise ValueError("allowed_emails must contain valid email addresses")
        return list(dict.fromkeys(emails))


class InstallBlueprintRequest(BaseModel):
    mode: InstallMode = InstallMode.SIMULATE
    workspace_name: Optional[str] = Field(None, max_length=200)
    share_token: Optional[str] = None
    create_missing_agents: bool = False
    agent_ids_by_service_key: dict[str, str] = Field(default_factory=dict)
    variable_values: dict[str, Any] = Field(default_factory=dict)
    channel_config_ids: dict[str, str] = Field(default_factory=dict)
    #: The author's never_allow entries are installed as approval gates unless
    #: the operator explicitly asks for the hard blocks.
    install_author_hard_blocks: bool = False
    governance_preset: str = Field(
        "automatic",
        pattern="^(automatic|follow_default|confirm_changes|risk_based)$",
        description="Overlay applied to the blueprint's governance policy.",
    )


class GovernancePresetSummary(BaseModel):
    key: str
    title: str
    summary: str


class InstallTodoResponse(BaseModel):
    kind: str
    detail: str
    payload: dict[str, Any]
    blocking: bool


class InstallResponse(BaseModel):
    workspace_id: str
    mode: str
    blueprint_id: Optional[str]
    blueprint_slug: Optional[str]
    stat_ids: list[str] = Field(default_factory=list)
    goal_ids: list[str]
    subscription_ids: list[str]
    scheduled_job_ids: list[str]
    workflow_binding_ids: list[str] = Field(default_factory=list)
    custom_field_ids: list[str]
    governance_applied: bool
    todos: list[InstallTodoResponse]
    notes: list[str]


class InstallPreflightResourceOptionResponse(BaseModel):
    id: str
    label: str


class InstallPreflightRequirementResponse(BaseModel):
    kind: str
    provider: str
    label: str
    required: bool
    blocking: bool
    ready: bool
    reason: str
    purpose: Optional[str] = None
    setup_kind: Optional[str] = None
    scope: Optional[str] = None
    config_fields_to_set: list[str] = Field(default_factory=list)
    requirement_key: Optional[str] = None
    resource_id: Optional[str] = None
    resource_options: list[InstallPreflightResourceOptionResponse] = Field(
        default_factory=list,
    )


class InstallPreflightResponse(BaseModel):
    ready: bool
    blocking_count: int
    requirements: list[InstallPreflightRequirementResponse] = Field(
        default_factory=list,
    )


class BlueprintStartupReconcileResponse(BaseModel):
    state: str
    dispatched_job_ids: list[str] = Field(default_factory=list)
    blocking_check_keys: list[str] = Field(default_factory=list)


class UnmetRequirementResponse(BaseModel):
    kind: str
    detail: str
    payload: dict[str, Any]


class ActivityResponse(BaseModel):
    total_steps: int
    by_status: dict[str, int]
    by_kind: dict[str, int]
    by_action_key: dict[str, int]
    governance_paused: int
    governance_denied: int


class CostResponse(BaseModel):
    total_credits: int
    total_usd: float
    by_kind_credits: dict[str, int]
    simulation_days: float
    daily_avg_credits: float
    projected_monthly_credits: int
    billed_credits: int
    credits_per_outcome: Optional[float]


class ErrorCountResponse(BaseModel):
    message: str
    count: int


class RunBreakdownResponse(BaseModel):
    key: str
    label: str
    total: int
    succeeded: int
    failed: int
    success_rate: Optional[float]
    judged: int


class DailyOutcomeResponse(BaseModel):
    day: str
    succeeded: int
    failed: int


class RunOutcomesResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    cancelled: int
    in_flight: int
    retried: int
    success_rate: Optional[float]
    avg_duration_seconds: Optional[float]
    top_errors: list[ErrorCountResponse]
    breakdown: list[RunBreakdownResponse]
    below_target: list[RunBreakdownResponse]
    daily: list[DailyOutcomeResponse]


class HumanLoadResponse(BaseModel):
    requested: int
    approved: int
    denied: int
    expired: int
    pending: int
    per_day: float
    median_wait_minutes: Optional[float]
    oldest_pending_hours: Optional[float]
    accepted: int
    rejected: int


class OperationsResponse(BaseModel):
    tasks: RunOutcomesResponse
    agents: RunOutcomesResponse
    workflows: RunOutcomesResponse
    automations: RunOutcomesResponse
    human: HumanLoadResponse
    stalled_tasks: int
    stalled_workflow_runs: int
    auto_paused_automations: list[str]
    failing_automations: list[str]


class LaunchCheckResponse(BaseModel):
    key: str
    status: Literal["pass", "warn", "fail", "na"]
    reason: str
    values: dict[str, Any]
    details: list[str]


class LaunchReadinessResponse(BaseModel):
    verdict: Literal["ready", "at_risk", "blocked", "insufficient_data"]
    checks: list[LaunchCheckResponse]


class ValueEvidenceResponse(BaseModel):
    verdict: Literal["on_track", "concerns", "below_expectations", "unproven"]
    checks: list[LaunchCheckResponse]


class CounterfactualResponse(BaseModel):
    preset_key: str
    title: str
    allowed: int
    paused_for_hitl: int
    denied: int
    delta_blocked_vs_actual: int


class GoalPaceResponse(BaseModel):
    goal_id: str
    title: str
    metric_key: str
    target_value: Optional[float]
    baseline_value: Optional[float]
    first_measurement_value: Optional[float]
    last_measurement_value: Optional[float]
    measurement_count: int
    progress_fraction: Optional[float]


class SimulationReportResponse(BaseModel):
    workspace_id: str
    workspace_name: str
    in_simulation: bool
    governance_preset: Optional[str]
    window_start: Optional[datetime]
    window_end: datetime
    simulation_started_at: Optional[datetime]
    promoted_at: Optional[datetime]
    activity: ActivityResponse
    cost: CostResponse
    counterfactuals: list[CounterfactualResponse]
    goals: list[GoalPaceResponse]
    operations: OperationsResponse
    readiness: LaunchReadinessResponse
    value: ValueEvidenceResponse
    notes: list[str]


class PromoteRequest(BaseModel):
    force: bool = False


class PromoteResponse(BaseModel):
    workspace_id: str
    promoted: bool
    unmet: list[UnmetRequirementResponse]
    notes: list[str]


class InstallPayloadPreflightRequest(BaseModel):
    """Read-only setup request for a portable JSON payload."""

    payload: dict[str, Any]
    mode: InstallMode = InstallMode.LIVE
    variable_values: dict[str, Any] = Field(default_factory=dict)
    channel_config_ids: dict[str, str] = Field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────

def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: Any) -> list[str]:
    return [item for item in _as_list(value) if isinstance(item, str) and item]


def _setup_item_from_variable(item: Any) -> BlueprintSetupItem | None:
    row = _as_record(item)
    label = _string_or_none(row.get("label")) or _string_or_none(row.get("key"))
    if not label:
        return None
    return BlueprintSetupItem(
        label=label,
        key=_string_or_none(row.get("key")),
        kind="variable",
        required=bool(row.get("required", False)),
        purpose=_string_or_none(row.get("purpose")),
        default=row.get("default"),
    )


def _setup_item_from_channel(item: Any) -> BlueprintSetupItem | None:
    row = _as_record(item)
    channel_type = _string_or_none(row.get("channel_type")) or _string_or_none(row.get("type"))
    if not channel_type:
        return None
    return BlueprintSetupItem(
        label=channel_type.replace("_", " ").title(),
        kind=channel_type,
        required=bool(row.get("required", True)),
        purpose=_string_or_none(row.get("purpose")),
    )


def _setup_item_from_session(item: Any) -> BlueprintSetupItem | None:
    row = _as_record(item)
    provider = _string_or_none(row.get("provider"))
    if not provider:
        return None
    label = _string_or_none(row.get("label"))
    provider_label = provider.replace("_", " ").title()
    return BlueprintSetupItem(
        label=f"{provider_label}{f' · {label}' if label else ''}",
        kind=provider,
        required=bool(row.get("required", True)),
        purpose=_string_or_none(row.get("purpose")),
    )


def _setup_item_from_integration(item: Any) -> BlueprintSetupItem | None:
    row = _as_record(item)
    provider = _string_or_none(row.get("slug"))
    if not provider:
        return None
    label = (
        _string_or_none(row.get("display_name"))
        or _string_or_none(row.get("name"))
        or provider.replace("_", " ").title()
    )
    return BlueprintSetupItem(
        label=label,
        kind=provider,
        required=bool(row.get("required", True)),
        purpose=_string_or_none(row.get("purpose")),
    )


def _setup_item_from_service(item: Any) -> BlueprintSetupItem | None:
    row = _as_record(item)
    label = _string_or_none(row.get("name")) or _string_or_none(row.get("key"))
    if not label:
        return None
    return BlueprintSetupItem(
        label=label,
        key=_string_or_none(row.get("key")),
        kind="service",
        purpose=_string_or_none(row.get("description")),
    )


def _split_required(items: list[BlueprintSetupItem]) -> tuple[list[BlueprintSetupItem], list[BlueprintSetupItem]]:
    required = [item for item in items if item.required]
    optional = [item for item in items if not item.required]
    return required, optional


def _payload_setup_preview(payload: dict[str, Any] | None) -> BlueprintSetupPreview:
    p = payload if isinstance(payload, dict) else {}
    manifest = _as_record(p.get("manifest"))
    contract = _as_record(p.get("contract"))
    policy = _as_record(p.get("policy"))
    recipe = _as_record(p.get("recipe"))
    operating_model = _as_record(recipe.get("operating_model"))
    requirements = _as_record(contract.get("requires"))
    expected = _as_record(policy.get("expected_baseline"))

    variables = [
        item
        for item in (_setup_item_from_variable(raw) for raw in _as_list(contract.get("variables")))
        if item is not None
    ]
    channels = [
        item
        for item in (
            _setup_item_from_channel(raw)
            for raw in (_as_list(contract.get("channels")) or _as_list(p.get("channel_requirements")))
        )
        if item is not None
    ]
    sessions = [
        item
        for item in (
            _setup_item_from_session(raw)
            for raw in (_as_list(contract.get("sessions")) or _as_list(p.get("session_requirements")))
        )
        if item is not None
    ]
    integrations = [
        item
        for item in (_setup_item_from_integration(raw) for raw in _as_list(requirements.get("mcp_servers")))
        if item is not None and is_user_facing_integration_provider(item.kind)
    ]
    services = [
        item
        for item in (_setup_item_from_service(raw) for raw in _as_list(operating_model.get("services")))
        if item is not None
    ]
    required_variables, optional_variables = _split_required(variables)
    required_channels, optional_channels = _split_required(channels)
    required_sessions, optional_sessions = _split_required(sessions)
    required_integrations, optional_integrations = _split_required(integrations)
    blocking = expected.get("blocking_todos_expected")

    return BlueprintSetupPreview(
        use_when=_string_or_none(manifest.get("use_when")),
        maturity_level=(
            _string_or_none(expected.get("maturity_level")) or _string_or_none(manifest.get("maturity_level"))
        ),
        validation_summary=_string_or_none(expected.get("validation_summary")),
        primary_work=_string_or_none(operating_model.get("primary_work")),
        runnable_in_simulation=bool(expected.get("runnable_in_simulation", False)),
        blocking_todos_expected=blocking if isinstance(blocking, int) else None,
        required_variables=required_variables,
        optional_variables=optional_variables,
        required_channels=required_channels,
        optional_channels=optional_channels,
        required_sessions=required_sessions,
        optional_sessions=optional_sessions,
        required_integrations=required_integrations,
        optional_integrations=optional_integrations,
        first_week_outputs=_string_list(expected.get("first_week_outputs")),
        validation_evidence=_string_list(expected.get("validation_evidence")),
        acceptance_criteria=_string_list(expected.get("acceptance_criteria")),
        not_included=_string_list(expected.get("not_included")),
        services=services,
        rules=_string_list(operating_model.get("rules")),
        author_hard_blocks=author_hard_blocks(p),
    )


def _showcase_assets(value: Any) -> list[BlueprintShowcaseAsset]:
    assets: list[BlueprintShowcaseAsset] = []
    for raw in _as_list(value):
        if not isinstance(raw, dict):
            continue
        try:
            assets.append(BlueprintShowcaseAsset.model_validate(raw))
        except Exception:
            logger.warning("ignoring malformed Blueprint showcase asset", exc_info=True)
    return assets


def _payload_author(payload: dict[str, Any] | None) -> dict[str, Any]:
    author = _manifest(payload or {}).get("author")
    return author if isinstance(author, dict) else {}


def _cover_template(
    *,
    title: str,
    description: str | None,
    tags: list[str],
    identity: str,
) -> BlueprintCoverTemplateSpec:
    return BlueprintCoverTemplateSpec.model_validate(
        build_blueprint_cover_template(
            title=title,
            description=description,
            tags=tags,
            identity=identity,
        )
    )


def _listing_text(b: WorkspaceBlueprint, field: str, is_owner: bool) -> str | None:
    """A listing's text in the viewer's language, from its manifest translations.

    Only text the listing still takes from its manifest is translated; an
    edited listing keeps its own words, and its owner sees what they wrote.
    """
    value = getattr(b, field)
    manifest = _manifest(b.payload)
    if is_owner or not isinstance(manifest.get("i18n"), dict) or value != manifest.get(field):
        return value
    from packages.core.i18n import get_locale
    from packages.core.services.viewer_language import localized

    translated = localized(manifest, get_locale()).get(field)
    return translated if isinstance(translated, str) and translated.strip() else value


def _summary(
    b: WorkspaceBlueprint,
    viewer_entity_id: str,
    *,
    author_avatar_url: str | None = None,
    favorite_count: int = 0,
    is_favorited: bool = False,
    remix_count: int = 0,
) -> BlueprintSummary:
    is_owner = b.entity_id == viewer_entity_id
    author = _payload_author(b.payload)
    owner_fields: dict[str, Any] = {}
    return BlueprintSummary(
        id=b.id,
        slug=b.slug,
        title=_listing_text(b, "title", is_owner),
        summary=_listing_text(b, "summary", is_owner),
        tags=list(b.tags or []),
        status=b.status,
        install_count=b.install_count,
        payload_version=b.payload_version,
        content_version=b.content_version,
        source_workspace_id=b.source_workspace_id,
        cover_image_url=b.cover_image_url,
        showcase_assets=_showcase_assets(b.showcase_assets),
        cover_template=_cover_template(
            title=b.title,
            description=b.description or b.summary,
            tags=list(b.tags or []),
            identity=b.id,
        ),
        author_handle=b.author_handle or _string_or_none(author.get("handle")),
        author_display_name=(
            b.author_display_name
            or _string_or_none(author.get("display_name"))
            or "Manor creator"
        ),
        author_avatar_url=author_avatar_url,
        remixed_from_id=(
            b.remixed_from_id
            or _string_or_none(_manifest(b.payload).get("forked_from_id"))
        ),
        favorite_count=favorite_count,
        is_favorited=is_favorited,
        remix_count=remix_count,
        setup_preview=_payload_setup_preview(b.payload),
        created_at=b.created_at,
        updated_at=b.updated_at,
        published_at=b.published_at,
        price_cents=b.price_cents,
        list_price_cents=b.list_price_cents,
        currency=b.currency,
        purchase_count=b.purchase_count,
        # share-token existence is owner-only metadata.
        has_share_token=bool(b.share_token) if is_owner else False,
        share_allowed_emails=list(b.share_allowed_emails or []) if is_owner else [],
        is_owner=is_owner,
        **owner_fields,
    )


def _owner_detail_fields(
    row: WorkspaceBlueprint,
    viewer_entity_id: str,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    return fields


def _manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest")
    return manifest if isinstance(manifest, dict) else {}


def _builtin_id(slug: str) -> str:
    return f"{_BUILTIN_BLUEPRINT_PREFIX}{slug}"


def _marketplace_identity_ids(row: WorkspaceBlueprint) -> set[str]:
    """All durable and historical handles naming one Marketplace row."""
    identities = {row.id}
    if row.entity_id is None:
        identities.update({row.slug, _builtin_id(row.slug)})
    return {identity for identity in identities if identity}


def _builtin_payload_for_id(blueprint_id: str) -> tuple[str, dict[str, Any]] | None:
    slug = (
        blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
        if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
        else blueprint_id
    )
    try:
        return slug, get_solo_company_blueprint(slug)
    except KeyError:
        return None


def _user_display_name(user: User | None) -> str:
    if user is None:
        return "Marketplace user"
    full_name = " ".join(
        part.strip()
        for part in (user.first_name or "", user.last_name or "")
        if part and part.strip()
    )
    return user.display_name or full_name or user.email.split("@", 1)[0]


async def _marketplace_signals(
    db: AsyncSession,
    blueprints: list[WorkspaceBlueprint | str],
    viewer_user_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    blueprint_ids = [
        blueprint.id if isinstance(blueprint, WorkspaceBlueprint) else blueprint
        for blueprint in blueprints
    ]
    signals = {
        blueprint_id: {
            "favorite_count": 0,
            "is_favorited": False,
            "remix_count": 0,
        }
        for blueprint_id in blueprint_ids
    }
    if not blueprint_ids:
        return signals

    rows_by_signal_id = {
        blueprint.id: blueprint
        for blueprint in blueprints
        if isinstance(blueprint, WorkspaceBlueprint)
    }
    unresolved_ids = [
        blueprint_id
        for blueprint_id in blueprint_ids
        if blueprint_id not in rows_by_signal_id
    ]
    if unresolved_ids:
        exact_rows = list((await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.id.in_(unresolved_ids),
            )
        )).scalars().all())
        exact_by_id = {row.id: row for row in exact_rows}
        rows_by_signal_id.update(exact_by_id)

        unresolved_slugs = {
            blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
            if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
            else blueprint_id
            for blueprint_id in unresolved_ids
            if blueprint_id not in exact_by_id
        }
        if unresolved_slugs:
            platform_rows = list((await db.execute(
                select(WorkspaceBlueprint).where(
                    WorkspaceBlueprint.entity_id.is_(None),
                    WorkspaceBlueprint.slug.in_(unresolved_slugs),
                )
            )).scalars().all())
            platform_by_slug = {row.slug: row for row in platform_rows}
            for blueprint_id in unresolved_ids:
                slug = (
                    blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
                    if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
                    else blueprint_id
                )
                if row := platform_by_slug.get(slug):
                    rows_by_signal_id[blueprint_id] = row

    canonical_by_alias = {
        blueprint_id: blueprint_id
        for blueprint_id in blueprint_ids
    }
    for blueprint_id, row in rows_by_signal_id.items():
        for alias in _marketplace_identity_ids(row):
            canonical_by_alias.setdefault(alias, blueprint_id)
    for blueprint_id in blueprint_ids:
        if blueprint_id in rows_by_signal_id:
            continue
        if _builtin_payload_for_id(blueprint_id) is not None:
            slug = (
                blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
                if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
                else blueprint_id
            )
            canonical_by_alias.setdefault(slug, blueprint_id)
            canonical_by_alias.setdefault(_builtin_id(slug), blueprint_id)
    identity_ids = list(canonical_by_alias)
    canonical_favorite_id = case(
        canonical_by_alias,
        value=BlueprintFavorite.blueprint_id,
    )
    favorite_rows = (await db.execute(
        select(
            canonical_favorite_id,
            func.count(func.distinct(BlueprintFavorite.user_id)),
        ).where(
            BlueprintFavorite.blueprint_id.in_(identity_ids),
        ).group_by(canonical_favorite_id)
    )).all()
    for blueprint_id, count in favorite_rows:
        if blueprint_id in signals:
            signals[blueprint_id]["favorite_count"] = int(count or 0)

    if viewer_user_id:
        viewer_favorites = (await db.execute(
            select(BlueprintFavorite.blueprint_id).where(
                BlueprintFavorite.blueprint_id.in_(identity_ids),
                BlueprintFavorite.user_id == viewer_user_id,
            )
        )).scalars().all()
        for blueprint_id in viewer_favorites:
            canonical_id = canonical_by_alias.get(blueprint_id)
            if canonical_id in signals:
                signals[canonical_id]["is_favorited"] = True

    canonical_remix_id = case(
        canonical_by_alias,
        value=WorkspaceBlueprint.remixed_from_id,
    )
    remix_rows = (await db.execute(
        select(
            canonical_remix_id,
            func.count(WorkspaceBlueprint.id),
        ).where(
            WorkspaceBlueprint.remixed_from_id.in_(identity_ids),
        ).group_by(canonical_remix_id)
    )).all()
    for blueprint_id, count in remix_rows:
        if blueprint_id in signals:
            signals[blueprint_id]["remix_count"] = int(count or 0)
    return signals


async def _author_avatars(
    db: AsyncSession,
    rows: list[WorkspaceBlueprint],
) -> dict[str, str | None]:
    user_ids = {row.author_user_id for row in rows if row.author_user_id}
    if not user_ids:
        return {}
    users = list((await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all())
    return {user.id: user.avatar_url for user in users}


async def _summary_with_signals(
    db: AsyncSession,
    row: WorkspaceBlueprint,
    viewer_entity_id: str,
    viewer_user_id: str | None = None,
) -> BlueprintSummary:
    signals = (
        await _marketplace_signals(db, [row], viewer_user_id)
    )[row.id]
    author_avatar_url = None
    if row.author_user_id:
        author = await db.get(User, row.author_user_id)
        author_avatar_url = author.avatar_url if author else None
    return _summary(
        row,
        viewer_entity_id,
        author_avatar_url=author_avatar_url,
        **signals,
    )


def _sniff_showcase_file(path: str) -> tuple[str, str, str] | None:
    with open(path, "rb") as handle:
        head = handle.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg", ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif", ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image", "image/webp", ".webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"avif", b"avis"}:
            return "image", "image/avif", ".avif"
        if brand in {b"qt  "}:
            return "video", "video/quicktime", ".mov"
        return "video", "video/mp4", ".mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video", "video/webm", ".webm"
    return None


def _showcase_asset_relative_path(
    blueprint_id: str,
    asset_id: str,
    mime_type: str | None,
) -> str | None:
    extension = _SHOWCASE_MIME_EXTENSIONS.get(str(mime_type or "").lower())
    if extension is None:
        return None
    return f"marketplace/blueprints/{blueprint_id}/{asset_id}{extension}"


async def _persist_showcase_upload(
    *,
    entity_id: str,
    blueprint_id: str,
    upload: UploadFile,
) -> dict[str, Any]:
    try:
        assert_entity_filesystem_ready()
    except EntityFilesystemError as exc:
        raise HTTPException(503, f"File storage is unavailable: {exc}") from exc

    declared_ext = os.path.splitext(upload.filename or "")[1].lower()
    if (
        declared_ext not in _SHOWCASE_IMAGE_EXTENSIONS
        and declared_ext not in _SHOWCASE_VIDEO_EXTENSIONS
    ):
        raise HTTPException(415, "Showcase files must be an image or video")

    fd, tmp_path = tempfile.mkstemp(prefix="manor-blueprint-showcase-", suffix=".tmp")
    os.close(fd)
    total = 0
    hard_limit = BLUEPRINT_SHOWCASE_VIDEO_MAX_BYTES
    try:
        import aiofiles

        async with aiofiles.open(tmp_path, "wb") as handle:
            while chunk := await upload.read(256 * 1024):
                total += len(chunk)
                if total > hard_limit:
                    raise HTTPException(
                        413,
                        f"Showcase videos must be at most "
                        f"{BLUEPRINT_SHOWCASE_VIDEO_MAX_BYTES // (1024 * 1024)} MB",
                    )
                await handle.write(chunk)

        detected = _sniff_showcase_file(tmp_path)
        if detected is None:
            raise HTTPException(415, "The uploaded file is not a supported image or video")
        kind, mime_type, extension = detected
        if kind == "image" and total > BLUEPRINT_SHOWCASE_IMAGE_MAX_BYTES:
            raise HTTPException(
                413,
                f"Showcase images must be at most "
                f"{BLUEPRINT_SHOWCASE_IMAGE_MAX_BYTES // (1024 * 1024)} MB",
            )

        asset_id = generate_ulid()
        rel_path = _showcase_asset_relative_path(
            blueprint_id,
            asset_id,
            mime_type,
        ) or f"marketplace/blueprints/{blueprint_id}/{asset_id}{extension}"
        copy_entity_file_atomic(
            entity_id,
            rel_path,
            tmp_path,
            expected_size=total,
            allow_empty=False,
        )
        return {
            "id": asset_id,
            "kind": kind,
            "url": entity_fs_open_url(entity_id, rel_path),
            "mime_type": mime_type,
            "size_bytes": total,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _showcase_entity_rel_path(row: WorkspaceBlueprint, url: Any) -> str | None:
    """Entity path of a showcase file this Blueprint uploaded, else ``None``."""
    file_ref = parse_file_reference(url)
    if file_ref is None or not row.entity_id or file_ref.entity_id != row.entity_id:
        return None
    if not file_ref.fs_path.startswith(f"marketplace/blueprints/{row.id}/"):
        return None
    return file_ref.fs_path


def _remove_showcase_file(row: WorkspaceBlueprint, asset: dict[str, Any]) -> None:
    rel_path = _showcase_entity_rel_path(row, asset.get("url"))
    if rel_path is None:
        return
    full_path = resolve_path(row.entity_id, rel_path)
    if full_path and os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except OSError:
            logger.warning("could not remove Blueprint showcase file %s", full_path)


async def _require_published_blueprint(
    db: AsyncSession,
    blueprint_id: str,
) -> WorkspaceBlueprint:
    row = await _find_blueprint_row(db, blueprint_id)
    if row is None or row.status != BlueprintStatus.PUBLISHED:
        raise HTTPException(404, "Published Blueprint not found")
    return row


async def _completed_purchase(
    db: AsyncSession, blueprint_id: str, entity_id: str,
) -> Any | None:
    """Return the live purchase entitlement held by ``entity_id``."""
    purchase = None
    return purchase


async def _has_completed_purchase(
    db: AsyncSession, blueprint_id: str, entity_id: str,
) -> bool:
    """True when ``entity_id`` holds a live (completed) entitlement."""
    return await _completed_purchase(db, blueprint_id, entity_id) is not None


async def _paid_purchase_matching_payload(
    db: AsyncSession,
    payload: dict[str, Any],
    entity_id: str,
) -> tuple[Any, WorkspaceBlueprint, str | None] | None:
    """Return paid provenance for an exact owned snapshot/current release.

    This match is provenance metadata only. Cloud raw imports are plan-gated
    independently because client-supplied JSON cannot prove it was not copied
    from or trivially modified from paid Marketplace content.
    """
    rows: list[tuple[Any, WorkspaceBlueprint]] = []
    matches: dict[
        str,
        tuple[Any, WorkspaceBlueprint, str | None],
    ] = {}
    # Prefer the current published release for each Blueprint when it also
    # equals the receipt. Provenance is attached only when the payload resolves
    # to exactly one owned Blueprint identity.
    for purchase, blueprint in rows:
        if (
            blueprint.status == BlueprintStatus.PUBLISHED
            and blueprint.payload == payload
        ):
            matches[blueprint.id] = (
                purchase,
                blueprint,
                blueprint.content_version,
            )
    for purchase, blueprint in rows:
        if (
            blueprint.id not in matches
            and purchase.payload_snapshot == payload
        ):
            matches[blueprint.id] = (
                purchase,
                blueprint,
                purchase.blueprint_content_version,
            )
    return next(iter(matches.values())) if len(matches) == 1 else None


async def _find_blueprint_row(
    db: AsyncSession,
    blueprint_id: str,
) -> WorkspaceBlueprint | None:
    """Resolve a durable id or a historical platform slug to one DB row."""
    slug = (
        blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
        if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
        else blueprint_id
    )
    return await resolve_blueprint_row(
        db,
        blueprint_id=blueprint_id,
        blueprint_slug=slug or None,
    )


async def _published_platform_blueprint_matching_payload(
    db: AsyncSession,
    payload: dict[str, Any],
) -> WorkspaceBlueprint | None:
    """Return the official row only when the complete JSON payload is exact."""
    payload_slug = str(_manifest(payload).get("slug") or "").strip()
    if not payload_slug:
        return None
    row = await resolve_blueprint_row(
        db,
        blueprint_id=platform_blueprint_id(payload_slug),
        blueprint_slug=payload_slug,
    )
    if (
        row is None
        or row.entity_id is not None
        or row.status != BlueprintStatus.PUBLISHED
        or json.dumps(row.payload, sort_keys=True)
        != json.dumps(payload, sort_keys=True)
    ):
        return None
    return row


async def _load_blueprint(
    db: AsyncSession, blueprint_id: str, entity_id: str,
    *, allow_published: bool = True, share_token: Optional[str] = None,
    viewer: User | None = None,
) -> WorkspaceBlueprint:
    """Loader with tenant + visibility check. Owners always see their
    own; anyone authenticated sees published ones. A valid share token
    unlocks any status (unlisted distribution); when an email list is set,
    the recipient must have a matching verified email. A completed purchase
    keeps the blueprint visible to its buyer even after the seller
    archives/unpublishes it (spec §4.3/§5.3).

    The token/purchase grants apply only to ``allow_published=True`` loads:
    ``allow_published=False`` marks owner-mutation contexts (update, delete,
    pricing, share-token), where non-owners must always 404."""
    row = await _find_blueprint_row(db, blueprint_id)
    if row is None:
        raise HTTPException(404, "blueprint not found")
    if row.entity_id != entity_id:
        if not (allow_published and row.status == BlueprintStatus.PUBLISHED):
            token_ok = (
                allow_published
                and bool(share_token)
                and bool(row.share_token)
                and share_token == row.share_token
                and _share_recipient_allowed(row, viewer)
            )
            purchased_ok = False
            if not token_ok and allow_published:
                purchased_ok = await _has_completed_purchase(db, row.id, entity_id)
            if not token_ok and not purchased_ok:
                raise HTTPException(404, "blueprint not found")
    return row


def _share_recipient_allowed(row: WorkspaceBlueprint, viewer: User | None) -> bool:
    emails = row.share_allowed_emails or []
    if not emails:
        return True
    return bool(
        viewer
        and email_is_verified(viewer)
        and viewer.email.casefold() in emails
    )


def _require_blueprint_content_editable(row: WorkspaceBlueprint) -> None:
    """Keep reviewed Marketplace payloads stable for installs and remixes."""
    if row.status not in BLUEPRINT_EDITABLE_STATUSES:
        raise HTTPException(
            409,
            "blueprint content can only be changed while draft or archived",
        )


def _install_response(r: InstallResult) -> InstallResponse:
    return InstallResponse(
        workspace_id=r.workspace_id,
        mode=r.mode.value,
        blueprint_id=r.blueprint_id,
        blueprint_slug=r.blueprint_slug,
        stat_ids=list(r.stat_ids),
        goal_ids=list(r.goal_ids),
        subscription_ids=list(r.subscription_ids),
        scheduled_job_ids=list(r.scheduled_job_ids),
        workflow_binding_ids=list(r.workflow_binding_ids),
        custom_field_ids=list(r.custom_field_ids),
        governance_applied=r.governance_applied,
        todos=[
            InstallTodoResponse(
                kind=t.kind, detail=t.detail, payload=t.payload, blocking=t.blocking,
            )
            for t in r.todos
        ],
        notes=list(r.notes),
    )


async def _commit_and_start_installed_workspace(
    db: AsyncSession,
    *,
    result: InstallResult,
    entity_id: str,
    requested_by: str | None = None,
) -> None:
    """Commit a blueprint install, then start its appropriate runtime.

    A live install follows normal Workspace startup. A simulation install runs
    the Blueprint-owned persisted Chat scenario instead; dispatching the live
    Strategist there would mix real proposals into the training walkthrough.
    Startup dispatch is best-effort because the durable install has already
    committed by the time a broker or worker failure can occur.
    """
    await db.commit()
    blocking_todos = [todo for todo in result.todos if todo.blocking]
    if result.mode == InstallMode.SIMULATE:
        from packages.core.blueprints.simulation_runtime import (
            start_simulation_run,
        )
        from packages.core.models.workspace import Workspace

        try:
            workspace = (await db.execute(
                select(Workspace).where(
                    Workspace.id == result.workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )).scalar_one()
            await start_simulation_run(db, workspace=workspace)
            await db.commit()
            result.notes.append(
                "Blueprint simulation started in Workspace Chat."
            )
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception(
                "workspace %s was installed but its simulation did not start",
                result.workspace_id,
            )
            result.notes.append(
                "Simulation installed; open Workspace Chat to retry the run."
            )
        return

    # Setup can block execution while still requiring an immediate Strategist
    # input request. Its occurrence was staged with the installation transaction.
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_initial_review import dispatch_initial_workspace_review

    try:
        installed_workspace = await db.get(Workspace, result.workspace_id)
        if installed_workspace is not None:
            if await dispatch_initial_workspace_review(db, installed_workspace):
                result.notes.append("First Strategist assessment queued after install.")
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "workspace %s was installed but initial review dispatch failed",
            result.workspace_id,
        )
        result.notes.append("Workspace installed; initial review dispatch remains pending.")

    startup = None
    try:
        from packages.core.services.blueprint_startup_service import (
            reconcile_blueprint_startup,
        )

        startup = await reconcile_blueprint_startup(
            db,
            workspace_id=result.workspace_id,
            trigger="install",
            requested_by=requested_by,
        )
        if startup is not None and startup.dispatched_job_ids:
            result.notes.append(
                "Blueprint startup queued: "
                + ", ".join(startup.dispatched_job_ids)
                + "."
            )
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "workspace %s was installed but Blueprint startup reconciliation failed",
            result.workspace_id,
        )
        result.notes.append(
            "Workspace installed; Blueprint startup will retry from readiness monitoring."
        )

    # Blueprint-owned startup replaces the generic post-commit path, but Agent
    # introductions are still part of every live Workspace's Chat experience.
    if (
        blocking_todos
        or startup is None
        or startup.state != "not_configured"
    ):
        from packages.core.services.workspace_setup_service import (
            dispatch_workspace_agent_greetings_once,
        )

        try:
            greeting_dispatch = await dispatch_workspace_agent_greetings_once(
                db,
                workspace_id=result.workspace_id,
                entity_id=entity_id,
            )
            await db.commit()
            if greeting_dispatch.get("warnings"):
                result.notes.append(
                    "Workspace installed; Agent introductions could not be queued."
                )
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception(
                "workspace %s was installed but Agent greetings dispatch failed",
                result.workspace_id,
            )
            result.notes.append(
                "Workspace installed; Agent introductions could not be queued."
            )

    if blocking_todos:
        result.notes.append(
            f"Normal Workspace startup deferred until {len(blocking_todos)} blocking "
            "setup item(s) are complete."
        )
        return
    if startup is None or startup.state != "not_configured":
        return

    from packages.core.services.workspace_setup_service import (
        dispatch_workspace_post_commit,
        record_workspace_post_commit_dispatch_failure,
    )

    try:
        dispatch = await dispatch_workspace_post_commit(
            db,
            workspace_id=result.workspace_id,
            entity_id=entity_id,
            strategist_countdown_seconds=5,
        )
        await db.commit()
        if dispatch.get("strategist_dispatched"):
            result.notes.append("First Strategist proposal queued after install.")
    except Exception as dispatch_exc:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "workspace %s was installed but startup dispatch failed",
            result.workspace_id,
        )
        try:
            await record_workspace_post_commit_dispatch_failure(
                db,
                workspace_id=result.workspace_id,
                entity_id=entity_id,
                error=dispatch_exc,
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception(
                "could not record startup dispatch failure for %s",
                result.workspace_id,
            )


async def _increment_blueprint_install_count(
    db: AsyncSession,
    blueprint_id: str,
) -> None:
    """Increment in SQL so concurrent successful installs cannot overwrite."""
    await db.execute(
        update(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.id == blueprint_id)
        .values(install_count=WorkspaceBlueprint.install_count + 1)
        .execution_options(synchronize_session=False)
    )


# ── Export (workspace → portable payload / draft blueprint row) ───────

async def _export_workspace_payload(
    db: AsyncSession,
    *,
    workspace_id: str,
    workspace_name: str,
    req: ExportBlueprintPayloadRequest,
    user: User,
    install_variables: tuple[dict[str, Any], ...] | None = None,
    file_embedder: Any = None,
) -> dict[str, Any]:
    context = ExportContext(
        include_subscriptions=req.include_subscriptions,
        include_goals=req.include_goals,
        include_stats=req.include_stats,
        include_scheduled_jobs=req.include_scheduled_jobs,
        include_workflows=req.include_workflows,
        include_blocking_setup=req.include_blocking_setup,
        include_custom_fields=req.include_custom_fields,
        include_governance=req.include_governance,
        include_channel_requirements=req.include_channel_requirements,
        include_session_requirements=req.include_session_requirements,
        include_embedded_agents=req.include_embedded_agents,
        include_embedded_skills=req.include_embedded_skills,
        include_knowledge_packs=req.include_knowledge_packs,
        knowledge_pack_mode=req.knowledge_pack_mode,
        knowledge_document_ids=(
            frozenset(req.knowledge_document_ids)
            if req.knowledge_document_ids is not None
            else None
        ),
        include_starter_memory=req.include_starter_memory,
        include_memory_files=req.include_memory_files,
        install_variables=install_variables,
        file_embedder=file_embedder,
    )
    author_display_name = req.author_display_name or _user_display_name(user)
    try:
        return await export_workspace(
            db,
            workspace_id,
            title=req.title or workspace_name,
            summary=req.summary,
            description=req.description,
            usage_guide=req.usage_guide,
            tags=req.tags,
            author_handle=req.author_handle,
            author_display_name=author_display_name,
            context=context,
        )
    except ExportError as exc:
        raise HTTPException(400, str(exc)) from exc

@workspace_router.get(
    "/{workspace_id}/blueprint-export/knowledge-documents",
    response_model=list[BlueprintExportKnowledgeDocument],
)
async def list_blueprint_export_knowledge_documents(
    workspace_id: str,
    limit: int = Query(BLUEPRINT_KNOWLEDGE_LIST_PAGE_SIZE, ge=1, le=BLUEPRINT_KNOWLEDGE_LIST_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List safe Workspace Knowledge files eligible as starter content."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    await require_workspace_writable(db, user, workspace_id)
    return await list_exportable_knowledge_documents(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
        limit=limit,
        offset=offset,
    )


@workspace_router.put("/{workspace_id}/blueprint-export/knowledge-documents/{document_id}/initial-template")
async def configure_blueprint_initial_template(
    workspace_id: str,
    document_id: str,
    req: BlueprintInitialTemplateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Declare portable initial content without changing the working file."""
    from packages.core.blueprints.exporter import (
        _document_is_inline_text, _document_portable_key, portable_document_classifications,
    )
    from packages.core.blueprints.knowledge_templates import INITIAL_BODY_KEY, normalize_knowledge_template
    from packages.core.blueprints.upgrade import (
        BlueprintUpgradeAccessDeniedError, _require_document_access,
    )
    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.permission import Capability
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
    ws = await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=user.entity_id)
    if ws is None or ws.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await lock_workspace_recipient_authorization(
        db, workspace_id=workspace_id, entity_id=user.entity_id, user_id=user.id,
    )
    await require_workspace_writable(db, user, workspace_id)
    if not await user_can_manage_workspace(
        db, workspace_id=workspace_id, user_id=user.id, entity_role=user.role,
    ):
        raise HTTPException(403, "Only Workspace managers can configure initial templates")
    doc = (await db.execute(select(Document).where(
        Document.id == document_id, Document.entity_id == user.entity_id,
        Document.is_trashed.is_(False),
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404, "Knowledge document not found")
    owners = set((await db.scalars(select(DocumentGroup.workspace_id).join(
        DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id,
    ).where(
        DocumentGroupMember.document_id == document_id, DocumentGroup.entity_id == user.entity_id,
    ))).all())
    if owners != {workspace_id}:
        raise HTTPException(409, "Initial templates require a Knowledge document exclusive to this Workspace")
    try:
        await _require_document_access(db, row=doc, actor=user, capability=Capability.EDIT)
    except BlueprintUpgradeAccessDeniedError as exc:
        raise HTTPException(403, str(exc)) from exc
    metadata = dict(doc.metadata_ or {})
    template = metadata.get("blueprint_template")
    if template is not None and (not isinstance(template, dict) or template.get("mode") != "initial_file"):
        raise HTTPException(409, "This document already has a different template contract")
    try:
        template = normalize_knowledge_template(template)
    except ValueError as exc:
        raise HTTPException(409, "This document has an invalid template contract") from exc
    if req.body_md is None:
        metadata.pop("blueprint_template", None)
        metadata.pop(INITIAL_BODY_KEY, None)
    else:
        if (
            doc.visibility == "private"
            or doc.classification not in portable_document_classifications(initial_template=True)
            or doc.pii_detected or doc.quarantine_status != "clean" or not _document_is_inline_text(doc)
        ):
            raise HTTPException(
                422,
                "Only clean Markdown or CSV Knowledge that is public or internal, not private, "
                "and has no detected personal data can declare an initial template",
            )
        if not template or metadata.get(INITIAL_BODY_KEY) != req.body_md:
            template = {
                "id": template["id"] if template else _document_portable_key(doc), "mode": "initial_file",
                "version": int((template or {}).get("version") or 0) + 1,
            }
        metadata.update({"blueprint_template": template, INITIAL_BODY_KEY: req.body_md})
    if metadata != doc.metadata_:
        doc.metadata_ = metadata
        await db.flush()
    return {
        "document_id": doc.id,
        "template": metadata.get("blueprint_template"),
        "content_source": "initial_template" if req.body_md is not None else "current_content",
    }


async def _export_authorized_workspace_payload(
    workspace_id: str,
    req: ExportBlueprintPayloadRequest,
    user: User,
    db: AsyncSession,
    *,
    file_embedder: Any = None,
) -> dict[str, Any]:
    # Lock the Workspace lifecycle boundary without ``get_workspace()``, whose
    # artifact-folder repair is a write. The lock also prevents a concurrent
    # soft delete from making this export outlive its source Workspace.
    workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    if workspace is None or workspace.deleted_at is not None:
        raise HTTPException(404, "Workspace not found")
    await lock_workspace_recipient_authorization(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
        user_id=user.id,
    )
    await require_workspace_writable(db, user, workspace_id)
    install_variables = (
        tuple(item.model_dump(exclude_none=True) for item in req.install_variables)
        if req.install_variables is not None
        else None
    )
    return await _export_workspace_payload(
        db,
        workspace_id=workspace_id,
        workspace_name=workspace.name,
        req=req,
        user=user,
        install_variables=install_variables,
        file_embedder=file_embedder,
    )


@workspace_router.post(
    "/{workspace_id}/export-blueprint-package",
    responses={200: {"content": {"application/gzip": {}}}},
)
async def export_workspace_blueprint_package(
    workspace_id: str,
    req: ExportBlueprintPayloadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The portable configuration plus the files its App and Skills point at
    (``blueprints.package``), as one ``.manor`` file."""
    from fastapi.responses import Response

    from packages.core.blueprints.package import FileEmbedder, PackageError, pack

    embedder = FileEmbedder(db, entity_id=user.entity_id, user=user, workspace_id=workspace_id)
    try:
        payload = await _export_authorized_workspace_payload(workspace_id, req, user, db, file_embedder=embedder)
        data = pack(payload, sorted(embedder.assets.values(), key=lambda asset: asset.sha256))
    except PackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    slug = re.sub(r"[^a-z0-9]+", "-", str(_manifest(payload).get("title") or "blueprint").lower()).strip("-")
    return Response(
        content=data, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{slug or "blueprint"}.manor"'},
    )


@workspace_router.post(
    "/{workspace_id}/export-blueprint-payload",
    response_model=dict[str, Any],
)
async def export_workspace_blueprint_payload(
    workspace_id: str,
    req: ExportBlueprintPayloadRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the validated portable configuration without publishing it."""
    return await _export_authorized_workspace_payload(workspace_id, req, user, db)


@workspace_router.post(
    "/{workspace_id}/export-blueprint",
    response_model=BlueprintDetail,
    status_code=201,
)
async def export_workspace_as_blueprint(
    workspace_id: str,
    req: ExportBlueprintRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Snapshot the workspace's configuration as a draft blueprint row.
    Operator can then edit metadata + publish via PUT/POST publish.

    Re-exporting a published Blueprint saves the snapshot as its next
    version; the live release keeps serving until that is approved."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    await require_workspace_writable(db, user, workspace_id)
    # Serialize first export/refreeze for this local Workspace. Without a row
    # lock, two requests can both observe no counterpart and mint two distinct
    # Marketplace ids before the provenance link exists.
    locked_ws = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        ).with_for_update()
    )).scalar_one_or_none()
    if locked_ws is None:
        raise HTTPException(404, "Workspace not found")
    ws = locked_ws

    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_PUBLISHED_AS,
        RESOURCE_WORKSPACE,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_WORKSPACE,
        marketplace_link_for_local_resource,
        record_marketplace_resource_link,
    )

    # Resolve the Marketplace counterpart by exact id. Historical exports can
    # be backfilled from source_workspace_id because it is itself an exact
    # local id; slug remains only a separately validated display locator.
    existing = None
    if req.marketplace_blueprint_id:
        existing = (await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.id == req.marketplace_blueprint_id,
                WorkspaceBlueprint.entity_id == user.entity_id,
                WorkspaceBlueprint.source_workspace_id == workspace_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            raise HTTPException(
                404,
                "Marketplace Blueprint counterpart not found for this Workspace",
            )
    else:
        counterpart = await marketplace_link_for_local_resource(
            db,
            entity_id=user.entity_id,
            local_resource_type=RESOURCE_WORKSPACE,
            local_resource_id=workspace_id,
            relationship=RELATIONSHIP_PUBLISHED_AS,
        )
        if counterpart is not None:
            existing = (await db.execute(
                select(WorkspaceBlueprint).where(
                    WorkspaceBlueprint.id == counterpart.marketplace_resource_id,
                    WorkspaceBlueprint.entity_id == user.entity_id,
                    WorkspaceBlueprint.source_workspace_id == workspace_id,
                )
            )).scalar_one_or_none()
            if existing is None:
                # A draft Blueprint could have been deleted before provenance
                # links were cleaned transactionally. Remove only this stale
                # published-as edge so the same Workspace can mint a new exact
                # counterpart below.
                await db.delete(counterpart)
                await db.flush()

    if existing is None:
        historical = list((await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.entity_id == user.entity_id,
                WorkspaceBlueprint.source_workspace_id == workspace_id,
            )
        )).scalars().all())
        if len(historical) == 1:
            existing = historical[0]
        elif len(historical) > 1:
            raise HTTPException(
                409,
                "This Workspace has multiple historical Marketplace Blueprints; "
                "retry with marketplace_blueprint_id",
            )

    slug_owner = (await db.execute(
        select(WorkspaceBlueprint).where(
            WorkspaceBlueprint.entity_id == user.entity_id,
            WorkspaceBlueprint.slug == req.slug,
        )
    )).scalar_one_or_none()
    if slug_owner is not None and (
        existing is None or slug_owner.id != existing.id
    ):
        raise HTTPException(409, f"blueprint slug {req.slug!r} already used")
    if existing is not None and not req.replace_existing:
        raise HTTPException(
            409,
            f"Workspace is already published as Blueprint {existing.id!r}",
        )
    stage_next_version = False
    if existing is not None:
        if not stage_next_version:
            _require_blueprint_content_editable(existing)
        if existing.source_workspace_id != workspace_id:
            raise HTTPException(
                409,
                "an existing blueprint with this slug came from another workspace",
            )

    install_variables = None
    if req.install_variables is not None:
        install_variables = tuple(
            item.model_dump(exclude_none=True)
            for item in req.install_variables
        )
    elif existing is not None:
        base_payload = existing.payload or {}
        install_variables = tuple(
            dict(item)
            for item in (base_payload.get("contract") or {}).get(
                "variables",
                [],
            )
            if isinstance(item, dict)
        )

    payload = await _export_workspace_payload(
        db,
        workspace_id=workspace_id,
        workspace_name=ws.name,
        req=req,
        user=user,
        install_variables=install_variables,
    )
    author_display_name = req.author_display_name or _user_display_name(user)

    source_blueprint = (
        (ws.settings or {}).get("_blueprint")
        if isinstance((ws.settings or {}).get("_blueprint"), dict)
        else {}
    )
    remixed_from_id = _string_or_none(source_blueprint.get("blueprint_id"))
    if remixed_from_id and remixed_from_id.startswith(DELIVERY_BLUEPRINT_ID_PREFIX):
        # A private client delivery is not public Marketplace lineage.
        remixed_from_id = None
    elif not remixed_from_id:
        source_slug = _string_or_none(source_blueprint.get("blueprint_slug"))
        if source_slug:
            remixed_from_id = _builtin_id(source_slug)
    manifest = dict(_manifest(payload))
    cover_image_url = (
        req.cover_image_url
        if req.cover_image_url is not None
        else (existing.cover_image_url if existing is not None else None)
    )
    manifest.update({
        "slug": req.slug,
        "cover_image_url": cover_image_url,
        "forked_from_id": remixed_from_id,
    })
    payload = {**payload, "manifest": manifest}

    payload_version = (
        (payload.get("manifest") or {}).get("blueprint_version")
        or payload.get("blueprint_version")
    )
    if existing is None:
        row = WorkspaceBlueprint(
            entity_id=user.entity_id,
            slug=req.slug,
            source_workspace_id=workspace_id,
            title=req.title,
            summary=req.summary,
            description=req.description,
            cover_image_url=cover_image_url,
            showcase_assets=[],
            tags=list(req.tags),
            author_user_id=user.id,
            author_handle=req.author_handle,
            author_display_name=author_display_name,
            remixed_from_id=remixed_from_id,
            payload=payload,
            payload_version=payload_version,
            status=BlueprintStatus.DRAFT.value,
        )
        db.add(row)
    else:
        row = existing
        listing = {
            "title": req.title,
            "summary": req.summary,
            "description": req.description,
            "cover_image_url": cover_image_url,
            "tags": list(req.tags),
            "author_handle": req.author_handle,
            "author_display_name": author_display_name,
            "remixed_from_id": remixed_from_id,
        }
        if not stage_next_version:
            row.slug = req.slug
            row.source_workspace_id = workspace_id
            for field, value in listing.items():
                setattr(row, field, value)
            row.status = BlueprintStatus.DRAFT.value
            row.published_at = None
            set_draft_payload(row, payload, payload_version=payload_version)
    await db.flush()
    await record_marketplace_resource_link(
        db,
        entity_id=user.entity_id,
        marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
        marketplace_resource_id=row.id,
        relationship=RELATIONSHIP_PUBLISHED_AS,
        scope_type=SCOPE_WORKSPACE,
        scope_id=workspace_id,
        local_resource_type=RESOURCE_WORKSPACE,
        local_resource_id=workspace_id,
        marketplace_version=row.content_version,
        linked_by=user.id,
        metadata={"source_slug": row.slug},
    )
    await db.refresh(row)
    detail = BlueprintDetail(
        **_summary(row, user.entity_id).model_dump(),
        description=row.description,
        payload=row.payload,
        **_owner_detail_fields(row, user.entity_id),
    )
    await db.commit()
    return detail


# ── List + detail + edit + delete ─────────────────────────────────────

@blueprint_router.get("", response_model=list[BlueprintSummary])
async def list_blueprints(
    status: Optional[str] = Query(
        None,
        pattern="^(draft|pending_review|published|archived)$",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List marketplace blueprints plus blueprints owned by the caller.

    Published DB blueprints are public marketplace entries after admin
    approval. Draft / pending_review / archived rows remain visible only
    to the owning entity.
    """
    if status == BlueprintStatus.PUBLISHED:
        visibility = WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED
    elif status:
        visibility = (
            (WorkspaceBlueprint.entity_id == user.entity_id)
            & (WorkspaceBlueprint.status == status)
        )
    else:
        visibility = (
            (WorkspaceBlueprint.entity_id == user.entity_id)
            | (WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED)
        )
    stmt = select(WorkspaceBlueprint).where(visibility).order_by(
        WorkspaceBlueprint.updated_at.desc().nulls_last(),
        WorkspaceBlueprint.created_at.desc(),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    # Platform blueprints are seeded into workspace_blueprints by the Cloud
    # deployment publisher and the single-machine OSS startup owner.
    # Appending the frozen configs here as well returns every platform entry
    # twice (with the same id), so the table is the list's single source.
    signals = await _marketplace_signals(db, rows, user.id)
    author_avatars = await _author_avatars(db, rows)
    summaries = [
        _summary(
            row,
            user.entity_id,
            author_avatar_url=author_avatars.get(row.author_user_id or ""),
            **signals.get(row.id, {}),
        )
        for row in rows
    ]
    return summaries


# Literal-path routes MUST come before the parameterised /{blueprint_id}
# route — otherwise FastAPI matches "governance-presets" as a blueprint id.
@blueprint_router.get(
    "/governance-presets",
    response_model=list[GovernancePresetSummary],
)
async def get_governance_presets(
    user: User = Depends(get_current_user),
):
    """Install-time governance overlays the operator can pick from."""
    return [
        GovernancePresetSummary(key=p.key, title=p.title, summary=p.summary)
        for p in list_presets()
    ]


@blueprint_router.get("/shared/{share_token}", response_model=BlueprintDetail)
async def resolve_shared_blueprint(
    share_token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a share link. Any authenticated user with the token can view
    the blueprint regardless of status (unlisted distribution)."""
    row = (await db.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.share_token == share_token)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "share link not found or revoked")
    if row.entity_id != user.entity_id and not _share_recipient_allowed(row, user):
        raise HTTPException(404, "share link not found or revoked")
    purchase = None
    if row.entity_id != user.entity_id:
        purchase = await _completed_purchase(db, row.id, user.entity_id)
    purchased = purchase is not None
    source_payload, _source_version = blueprint_delivery_source(row, purchase)
    summary = (
        await _summary_with_signals(db, row, user.entity_id, user.id)
    ).model_dump()
    if purchase is not None:
        summary["setup_preview"] = _payload_setup_preview(source_payload)
    return BlueprintDetail(
        **summary,
        description=_listing_text(row, "description", row.entity_id == user.entity_id),
        payload=source_payload,
        purchased=purchased,
    )


@blueprint_router.get("/{blueprint_id}", response_model=BlueprintDetail)
async def get_blueprint(
    blueprint_id: str,
    share_token: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _load_blueprint(
        db, blueprint_id, user.entity_id, share_token=share_token, viewer=user,
    )
    purchase = None
    if row.entity_id != user.entity_id:
        purchase = await _completed_purchase(db, row.id, user.entity_id)
    purchased = purchase is not None
    source_payload, _source_version = blueprint_delivery_source(row, purchase)
    summary = (
        await _summary_with_signals(db, row, user.entity_id, user.id)
    ).model_dump()
    if purchase is not None:
        summary["setup_preview"] = _payload_setup_preview(source_payload)
    can_notify_installers = False
    detail = BlueprintDetail(
        **summary,
        description=_listing_text(row, "description", row.entity_id == user.entity_id),
        payload=source_payload,
        purchased=purchased,
        can_notify_installers=can_notify_installers,
        **_owner_detail_fields(row, user.entity_id),
    )
    return detail


@blueprint_router.put("/{blueprint_id}", response_model=BlueprintSummary)
async def update_blueprint(
    blueprint_id: str,
    req: UpdateBlueprintRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be edited")

    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    _require_blueprint_content_editable(row)
    if req.title is not None:
        row.title = req.title
    if req.summary is not None:
        row.summary = req.summary
    if req.description is not None:
        row.description = req.description
    if req.usage_guide is not None:
        guide = req.usage_guide.strip()
    else:
        guide = _manifest(row.payload).get("usage_guide")
    if req.tags is not None:
        row.tags = list(req.tags)
    if req.cover_image_url is not None:
        row.cover_image_url = req.cover_image_url
    manifest = dict(_manifest(row.payload))
    manifest.update({
        "title": row.title,
        "summary": row.summary,
        "description": row.description,
        "usage_guide": guide,
        "tags": list(row.tags or []),
        "cover_image_url": row.cover_image_url,
    })
    set_listing_manifest(row, manifest)
    await db.flush()
    await db.refresh(row)
    summary = await _summary_with_signals(db, row, user.entity_id, user.id)
    await db.commit()
    return summary


@blueprint_router.post(
    "/{blueprint_id}/showcase-assets",
    response_model=BlueprintSummary,
    status_code=201,
)
async def upload_blueprint_showcase_asset(
    blueprint_id: str,
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    alt_text: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish an explicit public copy of an image/video for the listing."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be edited")
    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    _require_blueprint_content_editable(row)
    assets = [
        asset for asset in (row.showcase_assets or [])
        if isinstance(asset, dict)
    ]
    if len(assets) >= BLUEPRINT_SHOWCASE_MAX_ASSETS:
        raise HTTPException(
            409,
            f"A Blueprint can have at most {BLUEPRINT_SHOWCASE_MAX_ASSETS} showcase assets",
        )

    asset = await _persist_showcase_upload(
        entity_id=row.entity_id,
        blueprint_id=row.id,
        upload=file,
    )
    asset["caption"] = (caption or "").strip() or None
    asset["alt_text"] = (alt_text or "").strip() or None
    assets.append(asset)
    row.showcase_assets = assets
    if not row.cover_image_url and asset["kind"] == "image":
        row.cover_image_url = asset["url"]
    manifest = dict(_manifest(row.payload))
    manifest["cover_image_url"] = row.cover_image_url
    manifest["showcase_assets"] = assets
    set_listing_manifest(row, manifest)
    await db.flush()
    await db.refresh(row)
    summary = await _summary_with_signals(db, row, user.entity_id, user.id)
    await db.commit()
    return summary


@blueprint_router.delete(
    "/{blueprint_id}/showcase-assets/{asset_id}",
    response_model=BlueprintSummary,
)
async def delete_blueprint_showcase_asset(
    blueprint_id: str,
    asset_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be edited")
    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    _require_blueprint_content_editable(row)
    assets = [
        asset for asset in (row.showcase_assets or [])
        if isinstance(asset, dict)
    ]
    removed = next(
        (asset for asset in assets if str(asset.get("id") or "") == asset_id),
        None,
    )
    if removed is None:
        raise HTTPException(404, "Showcase asset not found")
    _remove_showcase_file(row, removed)
    remaining = [
        asset for asset in assets
        if str(asset.get("id") or "") != asset_id
    ]
    row.showcase_assets = remaining
    if row.cover_image_url == removed.get("url"):
        row.cover_image_url = next(
            (
                str(asset["url"])
                for asset in remaining
                if asset.get("kind") == "image" and asset.get("url")
            ),
            None,
        )
    manifest = dict(_manifest(row.payload))
    manifest["cover_image_url"] = row.cover_image_url
    manifest["showcase_assets"] = remaining
    set_listing_manifest(row, manifest)
    await db.flush()
    await db.refresh(row)
    summary = await _summary_with_signals(db, row, user.entity_id, user.id)
    await db.commit()
    return summary


@blueprint_router.post(
    "/{blueprint_id}/favorite",
    response_model=BlueprintFavoriteResponse,
)
async def toggle_blueprint_favorite(
    blueprint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    slug = (
        blueprint_id[len(_BUILTIN_BLUEPRINT_PREFIX):]
        if blueprint_id.startswith(_BUILTIN_BLUEPRINT_PREFIX)
        else blueprint_id
    )
    row = await db.get(WorkspaceBlueprint, blueprint_id)
    if row is None and slug:
        row = (await db.execute(
            select(WorkspaceBlueprint).where(
                WorkspaceBlueprint.entity_id.is_(None),
                WorkspaceBlueprint.slug == slug,
            )
        )).scalars().first()
    if row is None:
        row = await _require_published_blueprint(db, blueprint_id)
    elif row.status != BlueprintStatus.PUBLISHED:
        raise HTTPException(404, "Published Blueprint not found")

    identity_ids = (
        _marketplace_identity_ids(row)
        if row is not None
        else {blueprint_id, slug, _builtin_id(slug)}
    )
    canonical_id = row.id if row is not None else blueprint_id
    favorites = list((await db.execute(
        select(BlueprintFavorite).where(
            BlueprintFavorite.blueprint_id.in_(identity_ids),
            BlueprintFavorite.user_id == user.id,
        )
    )).scalars().all())
    if not favorites:
        db.add(BlueprintFavorite(
            blueprint_id=canonical_id,
            entity_id=user.entity_id,
            user_id=user.id,
        ))
        is_favorited = True
    else:
        for favorite in favorites:
            await db.delete(favorite)
        is_favorited = False
    await db.commit()
    favorite_count = int((await db.execute(
        select(func.count(func.distinct(BlueprintFavorite.user_id))).where(
            BlueprintFavorite.blueprint_id.in_(identity_ids),
        )
    )).scalar_one())
    return BlueprintFavoriteResponse(
        is_favorited=is_favorited,
        favorite_count=favorite_count,
    )




@blueprint_router.post("/{blueprint_id}/share-token", response_model=ShareTokenResponse)
async def create_or_rotate_share_token(
    blueprint_id: str,
    req: ShareTokenRequest | None = Body(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or rotate the blueprint's share token (owner only)."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in blueprints cannot be link-shared")
    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    if req is not None and req.allowed_emails is not None:
        row.share_allowed_emails = req.allowed_emails
    row.share_token = secrets.token_urlsafe(32)
    await db.flush()
    token = row.share_token
    await db.commit()
    return ShareTokenResponse(share_token=token)


@blueprint_router.delete("/{blueprint_id}/share-token", status_code=204)
async def revoke_share_token(
    blueprint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke link sharing (owner only)."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in blueprints cannot be link-shared")
    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    row.share_token = None
    row.share_allowed_emails = []
    await db.commit()






@blueprint_router.delete("/{blueprint_id}", status_code=204)
async def delete_blueprint(
    blueprint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be deleted")

    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    row = (await db.execute(
        select(WorkspaceBlueprint)
        .where(
            WorkspaceBlueprint.id == row.id,
            WorkspaceBlueprint.entity_id == user.entity_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "blueprint not found")
    _require_blueprint_content_editable(row)
    purchase_id = None
    if purchase_id is not None:
        raise HTTPException(
            409,
            "Blueprints with purchase history cannot be deleted; archive it instead.",
        )
    for asset in row.showcase_assets or []:
        if isinstance(asset, dict):
            _remove_showcase_file(row, asset)
    await db.execute(
        delete(BlueprintFavorite).where(BlueprintFavorite.blueprint_id == row.id)
    )
    from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_PUBLISHED_AS,
        RESOURCE_WORKSPACE_BLUEPRINT,
    )

    await db.execute(
        delete(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == user.entity_id,
            MarketplaceResourceLink.marketplace_resource_type
            == RESOURCE_WORKSPACE_BLUEPRINT,
            MarketplaceResourceLink.marketplace_resource_id == row.id,
            MarketplaceResourceLink.relationship == RELATIONSHIP_PUBLISHED_AS,
        )
    )
    await db.delete(row)
    await db.commit()


# ── Install ───────────────────────────────────────────────────────────

def _install_preflight_response(
    result: BlueprintSetupPreflight,
    *,
    mode: InstallMode = InstallMode.LIVE,
) -> InstallPreflightResponse:
    blocking_requirements = result.blocking_requirements_for_mode(mode)
    return InstallPreflightResponse(
        ready=result.ready_for_mode(mode),
        blocking_count=len(blocking_requirements),
        requirements=[
            InstallPreflightRequirementResponse(
                kind=requirement.kind.value,
                provider=requirement.provider,
                label=requirement.label,
                required=requirement.required,
                blocking=requirement in blocking_requirements,
                ready=requirement.ready,
                reason=requirement.reason,
                purpose=requirement.purpose,
                setup_kind=requirement.setup_kind,
                scope=requirement.scope,
                config_fields_to_set=list(requirement.config_fields_to_set),
                requirement_key=requirement.requirement_key,
                resource_id=requirement.resource_id,
                resource_options=[
                    InstallPreflightResourceOptionResponse(
                        id=option.id,
                        label=option.label,
                    )
                    for option in requirement.resource_options
                ],
            )
            for requirement in result.requirements
        ],
    )


async def _require_install_preflight(
    db: AsyncSession,
    *,
    payload: dict[str, Any],
    entity_id: str,
    user_id: str,
    mode: InstallMode = InstallMode.LIVE,
    selected_channel_config_ids: dict[str, str] | None = None,
    variable_values: dict[str, Any] | None = None,
) -> BlueprintSetupPreflight:
    try:
        setup_payload, _ = resolve_install_variables(
            migrate_payload(payload),
            variable_values,
        )
    except PayloadError as exc:
        logger.warning("Blueprint setup preflight rejected a payload", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_PREFLIGHT_INVALID_DETAIL) from exc
    except InstallError as exc:
        logger.warning(
            "Blueprint install rejected invalid configuration",
            exc_info=True,
        )
        raise HTTPException(
            400,
            _BLUEPRINT_CONFIGURATION_INVALID_DETAIL,
        ) from exc
    try:
        result = await BlueprintSetupPreflightFactory.from_payload(
            db,
            payload=setup_payload,
            entity_id=entity_id,
            user_id=user_id,
            selected_channel_config_ids=selected_channel_config_ids,
        )
    except BlueprintSetupPreflightError as exc:
        logger.warning("Blueprint setup preflight rejected a payload", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_PREFLIGHT_INVALID_DETAIL) from exc
    if not result.ready_for_mode(mode):
        response = _install_preflight_response(result, mode=mode)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "blueprint_setup_required",
                "message": (
                    "Connect all required Blueprint integrations, channels, and sessions "
                    "before creating the Workspace."
                ),
                "preflight": response.model_dump(),
            },
        )
    return result


@blueprint_router.get(
    "/{blueprint_id}/install-preflight",
    response_model=InstallPreflightResponse,
)
async def install_preflight(
    blueprint_id: str,
    share_token: Optional[str] = Query(None),
    variable_values: Optional[str] = Query(None),
    mode: InstallMode = Query(InstallMode.LIVE),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve creator-declared account dependencies without writing state."""
    row = await _load_blueprint(
        db,
        blueprint_id,
        user.entity_id,
        share_token=share_token,
        viewer=user,
    )
    purchase = None
    if row.entity_id != user.entity_id:
        purchase = await _completed_purchase(db, row.id, user.entity_id)
    payload, _source_version = blueprint_delivery_source(row, purchase)
    try:
        parsed_variable_values = json.loads(variable_values) if variable_values else {}
        if not isinstance(parsed_variable_values, dict):
            raise ValueError("variable_values must be an object")
        setup_payload, _ = resolve_install_variables(
            migrate_payload(payload),
            parsed_variable_values,
        )
        result = await BlueprintSetupPreflightFactory.from_payload(
            db,
            payload=setup_payload,
            entity_id=user.entity_id,
            user_id=user.id,
        )
    except (
        BlueprintSetupPreflightError,
        InstallError,
        PayloadError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("Blueprint install preflight rejected a payload", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_PREFLIGHT_INVALID_DETAIL) from exc
    return _install_preflight_response(result, mode=mode)


@blueprint_router.post(
    "/install-payload-preflight",
    response_model=InstallPreflightResponse,
)
async def install_payload_preflight(
    req: InstallPayloadPreflightRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview raw-payload setup without creating a Workspace.

    The install route repeats this check under its normal transaction, so this
    endpoint is guidance only and cannot be used to bypass current access.
    """
    try:
        setup_payload, _ = resolve_install_variables(
            migrate_payload(req.payload), req.variable_values,
        )
        result = await BlueprintSetupPreflightFactory.from_payload(
            db,
            payload=setup_payload,
            entity_id=user.entity_id,
            user_id=user.id,
            selected_channel_config_ids=req.channel_config_ids,
        )
    except (BlueprintSetupPreflightError, InstallError, PayloadError) as exc:
        logger.warning("Blueprint payload preflight rejected a payload", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_PREFLIGHT_INVALID_DETAIL) from exc
    return _install_preflight_response(result, mode=req.mode)


@blueprint_router.post(
    "/{blueprint_id}/install",
    response_model=InstallResponse,
    status_code=201,
)
async def install(
    blueprint_id: str,
    req: InstallBlueprintRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _load_blueprint(
        db, blueprint_id, user.entity_id, share_token=req.share_token, viewer=user,
    )
    purchase = None
    if row.entity_id != user.entity_id:
        purchase = await _completed_purchase(db, row.id, user.entity_id)
    if (
        row.entity_id != user.entity_id
        and blueprint_delivery_is_paid(row, purchase)
    ):
        if purchase is None:
            raise HTTPException(402, "purchase required to install this blueprint")
    # Published installs follow the current supported release.  A completed
    # purchase snapshot remains the fallback when that release is no longer
    # published, so archival cannot destroy the product the buyer paid for.
    payload, source_version = blueprint_delivery_source(row, purchase)
    setup_preflight = await _require_install_preflight(
        db,
        payload=payload,
        entity_id=user.entity_id,
        user_id=user.id,
        mode=req.mode,
        selected_channel_config_ids=req.channel_config_ids,
        variable_values=req.variable_values,
    )
    try:
        result = await install_blueprint(
            db,
            entity_id=user.entity_id,
            payload=payload,
            mode=req.mode,
            workspace_name=req.workspace_name,
            user_id=user.id,
            blueprint_id=row.id,
            blueprint_slug=row.slug,
            blueprint_version=source_version,
            create_missing_agents=req.create_missing_agents,
            agent_ids_by_service_key=req.agent_ids_by_service_key,
            governance_preset=req.governance_preset,
            install_author_hard_blocks=req.install_author_hard_blocks,
            variable_values=req.variable_values,
            channel_config_ids={
                requirement.requirement_key: requirement.resource_id
                for requirement in setup_preflight.requirements
                if requirement.requirement_key and requirement.resource_id
            } | req.channel_config_ids,
        )
    except InstallError as exc:
        logger.warning("Blueprint install rejected invalid configuration", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_CONFIGURATION_INVALID_DETAIL) from exc

    await _increment_blueprint_install_count(db, row.id)
    await _commit_and_start_installed_workspace(
        db,
        result=result,
        entity_id=user.entity_id,
        requested_by=user.id,
    )
    response = _install_response(result)
    return response


# ── Install from raw payload (no DB row required) ─────────────────────
# Useful for previewing a freshly-exported payload OR for tooling that
# generates blueprints externally without round-tripping through the
# blueprint table.
#
# Cloud raw imports require an active paid plan. A Workspace source can instead
# prove origin through a fresh authorized server export, held through install.
# Matching arbitrary JSON to a Marketplace listing is not proof of entitlement.

@blueprint_router.post(
    "/install-payload",
    response_model=InstallResponse,
    status_code=201,
)
async def install_from_payload(
    payload: dict[str, Any] = Body(...),
    mode: InstallMode = Body(InstallMode.SIMULATE),
    workspace_name: Optional[str] = Body(None),
    create_missing_agents: bool = Body(False),
    agent_ids_by_service_key: dict[str, str] = Body(default_factory=dict),
    governance_preset: str = Body("automatic"),
    install_author_hard_blocks: bool = Body(False),
    variable_values: dict[str, Any] = Body(default_factory=dict),
    channel_config_ids: dict[str, str] = Body(default_factory=dict),
    workspace_source: Optional[WorkspaceBlueprintPayloadSource] = Body(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if workspace_source is not None:
        from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

        # The installer also needs this lock. Acquire it before the source row
        # to match Workspace deletion and operating-model updates; taking it
        # after export would invert their lifecycle -> Workspace lock order.
        await lock_reusable_resource_lifecycle(db, entity_id=user.entity_id)
        source_payload = await _export_authorized_workspace_payload(
            workspace_source.workspace_id, workspace_source.export_options, user, db,
        )
        # Compare the entire document, including metadata and JSON value types.
        # Never use a client assertion of origin or the looser upgrade fingerprint
        # as authority. A stale/edited export must be exported again or imported
        # through the ordinary raw-payload gate.
        if json.dumps(payload, sort_keys=True) != json.dumps(source_payload, sort_keys=True):
            raise HTTPException(409, detail={
                "code": "blueprint_workspace_source_mismatch",
                "message": "Payload does not match the current Workspace export. Export it again before installing.",
            })
        payload = source_payload

    payload_slug = str(_manifest(payload).get("slug") or "").strip()
    paid_source = (
        await _paid_purchase_matching_payload(db, payload, user.entity_id)
        if workspace_source is None else None
    )
    paid_purchase = paid_source[0] if paid_source else None
    paid_blueprint_id = paid_purchase.blueprint_id if paid_purchase else None
    from packages.core.services.marketplace_billing import (
        MarketplacePaidPlanRequiredError,
        require_paid_marketplace_plan,
    )
    try:
        if workspace_source is None:
            await require_paid_marketplace_plan(
                db,
                entity_id=user.entity_id,
                blueprint_id=paid_blueprint_id,
            )
    except MarketplacePaidPlanRequiredError as exc:
        raise HTTPException(402, detail=exc.detail) from exc

    # A Workspace snapshot is independent configuration, not a Marketplace
    # install/upgrade receipt, even if a listing happens to have equal content.
    matched = None if workspace_source is not None else (
        paid_source[1]
        if paid_source
        else await _published_platform_blueprint_matching_payload(db, payload)
    )
    setup_preflight = await _require_install_preflight(
        db,
        payload=payload,
        entity_id=user.entity_id,
        user_id=user.id,
        mode=mode,
        selected_channel_config_ids=channel_config_ids,
        variable_values=variable_values,
    )
    try:
        result = await install_blueprint(
            db,
            entity_id=user.entity_id,
            payload=payload,
            mode=mode,
            workspace_name=workspace_name,
            user_id=user.id,
            blueprint_id=matched.id if matched else None,
            blueprint_slug=(matched.slug if matched else payload_slug) or None,
            blueprint_version=(
                paid_source[2]
                if paid_source is not None
                else matched.content_version if matched else None
            ),
            create_missing_agents=create_missing_agents,
            agent_ids_by_service_key=agent_ids_by_service_key,
            governance_preset=governance_preset,
            install_author_hard_blocks=install_author_hard_blocks,
            variable_values=variable_values,
            channel_config_ids={
                requirement.requirement_key: requirement.resource_id
                for requirement in setup_preflight.requirements
                if requirement.requirement_key and requirement.resource_id
            } | channel_config_ids,
        )
    except InstallError as exc:
        logger.warning("Blueprint payload install rejected invalid configuration", exc_info=True)
        raise HTTPException(400, _BLUEPRINT_CONFIGURATION_INVALID_DETAIL) from exc
    await _commit_and_start_installed_workspace(
        db,
        result=result,
        entity_id=user.entity_id,
        requested_by=user.id,
    )
    return _install_response(result)


class InstallPackageOptions(BaseModel):
    mode: InstallMode = InstallMode.SIMULATE
    workspace_name: Optional[str] = None
    create_missing_agents: bool = False
    agent_ids_by_service_key: dict[str, str] = Field(default_factory=dict)
    governance_preset: str = Field("automatic", pattern="^(automatic|follow_default|confirm_changes|risk_based)$")
    install_author_hard_blocks: bool = False
    variable_values: dict[str, Any] = Field(default_factory=dict)
    channel_config_ids: dict[str, str] = Field(default_factory=dict)


async def read_blueprint_package(file: UploadFile, entity_id: str) -> dict[str, Any]:
    """The package's payload with each carried file written into this entity
    and its new address in place of the placeholder."""
    from packages.core.blueprints.package import MAX_PACKAGE_BYTES, PackageError, restore_assets, store_assets, unpack

    data = await file.read(MAX_PACKAGE_BYTES + 1)
    try:
        payload, assets = unpack(data)
    except PackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    if assets:
        try:
            assert_entity_filesystem_ready()
        except EntityFilesystemError as exc:
            raise HTTPException(503, f"File storage is unavailable: {exc}") from exc
    return restore_assets(payload, await store_assets(entity_id, assets))


@blueprint_router.post(
    "/install-package",
    response_model=InstallResponse,
    status_code=201,
)
async def install_from_package(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Install a ``.manor`` package: its files first, then its payload as
    ``/install-payload`` does (same entitlement, preflight and setup)."""
    try:
        opts = InstallPackageOptions.model_validate_json(options or "{}")
    except ValueError as exc:
        raise HTTPException(422, f"Invalid install options: {exc}") from exc
    payload = await read_blueprint_package(file, user.entity_id)
    return await install_from_payload(
        payload=payload, mode=opts.mode, workspace_name=opts.workspace_name,
        create_missing_agents=opts.create_missing_agents,
        agent_ids_by_service_key=opts.agent_ids_by_service_key,
        governance_preset=opts.governance_preset,
        install_author_hard_blocks=opts.install_author_hard_blocks,
        variable_values=opts.variable_values, channel_config_ids=opts.channel_config_ids,
        workspace_source=None, user=user, db=db,
    )


# ── Blueprint startup repair ──────────────────────────────────────────

@workspace_router.post(
    "/{workspace_id}/blueprint-startup/reconcile",
    response_model=BlueprintStartupReconcileResponse,
)
async def reconcile_workspace_blueprint_startup(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Idempotently resume startup declared by this Workspace's Blueprint."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    await require_workspace_writable(db, user, workspace_id)

    from packages.core.services.blueprint_startup_service import (
        reconcile_missing_blueprint_startup_contract,
        reconcile_blueprint_startup,
    )

    blueprint_record = (
        (ws.settings or {}).get("_blueprint")
        if isinstance(ws.settings, dict)
        else None
    )
    installed_blueprint_id = str(
        (blueprint_record or {}).get("blueprint_id") or ""
    ).strip()
    builtin = (
        _builtin_payload_for_id(installed_blueprint_id)
        if installed_blueprint_id
        else None
    )
    if builtin is not None:
        contract = await reconcile_missing_blueprint_startup_contract(
            db,
            workspace=ws,
            payload=builtin[1],
            source_blueprint_id=_builtin_id(builtin[0]),
        )
        if contract.state == "jobs_missing":
            return BlueprintStartupReconcileResponse(
                state="contract_jobs_missing",
                dispatched_job_ids=[],
                blocking_check_keys=list(contract.missing_job_ids),
            )
        # The contract check locks the Workspace row. Dispatch claims its job in
        # a separate session, so release the lock first, changed or not;
        # otherwise the request waits on itself.
        await db.commit()

    result = await reconcile_blueprint_startup(
        db,
        workspace_id=workspace_id,
        trigger="api_repair",
        requested_by=user.id,
    )
    return BlueprintStartupReconcileResponse(
        state=result.state,
        dispatched_job_ids=list(result.dispatched_job_ids),
        blocking_check_keys=list(result.blocking_check_keys),
    )


# ── Simulation report (M12.4) ─────────────────────────────────────────

@workspace_router.get(
    "/{workspace_id}/simulation-report",
    response_model=SimulationReportResponse,
)
async def get_simulation_report(
    workspace_id: str,
    recent_days: Optional[int] = Query(
        None, ge=1, le=90,
        description="Evaluate only the most recent N days of the simulation.",
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Launch review + activity + cost + counterfactual + goal-pace digest
    of what the workspace did during simulation. Read-only — safe to call
    repeatedly while the operator is deciding whether to promote."""
    await require_workspace_readable(db, user, workspace_id)
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    try:
        report = await simulate_report(
            db, workspace_id, viewer_user_id=user.id, recent_days=recent_days,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return _serialise_report(report)


def _serialise_report(r: SimulationReport) -> SimulationReportResponse:
    return SimulationReportResponse(
        workspace_id=r.workspace_id,
        workspace_name=r.workspace_name,
        in_simulation=r.in_simulation,
        governance_preset=r.governance_preset,
        window_start=r.window_start,
        window_end=r.window_end,
        simulation_started_at=r.simulation_started_at,
        promoted_at=r.promoted_at,
        activity=ActivityResponse(
            total_steps=r.activity.total_steps,
            by_status=dict(r.activity.by_status),
            by_kind=dict(r.activity.by_kind),
            by_action_key=dict(r.activity.by_action_key),
            governance_paused=r.activity.governance_paused,
            governance_denied=r.activity.governance_denied,
        ),
        cost=CostResponse(
            total_credits=r.cost.total_credits,
            total_usd=r.cost.total_usd,
            by_kind_credits=dict(r.cost.by_kind_credits),
            simulation_days=r.cost.simulation_days,
            daily_avg_credits=r.cost.daily_avg_credits,
            projected_monthly_credits=r.cost.projected_monthly_credits,
            billed_credits=r.cost.billed_credits,
            credits_per_outcome=r.cost.credits_per_outcome,
        ),
        counterfactuals=[
            CounterfactualResponse(
                preset_key=c.preset_key,
                title=c.title,
                allowed=c.allowed,
                paused_for_hitl=c.paused_for_hitl,
                denied=c.denied,
                delta_blocked_vs_actual=c.delta_blocked_vs_actual,
            )
            for c in r.counterfactuals
        ],
        goals=[
            GoalPaceResponse(
                goal_id=g.goal_id,
                title=g.title,
                metric_key=g.metric_key,
                target_value=g.target_value,
                baseline_value=g.baseline_value,
                first_measurement_value=g.first_measurement_value,
                last_measurement_value=g.last_measurement_value,
                measurement_count=g.measurement_count,
                progress_fraction=g.progress_fraction,
            )
            for g in r.goals
        ],
        operations=OperationsResponse.model_validate(asdict(r.operations)),
        readiness=LaunchReadinessResponse.model_validate(asdict(r.readiness)),
        value=ValueEvidenceResponse.model_validate(asdict(r.value)),
        notes=list(r.notes),
    )


# ── Promote (sandbox → live) ──────────────────────────────────────────

@workspace_router.get(
    "/{workspace_id}/promote/preflight",
    response_model=list[UnmetRequirementResponse],
)
async def promote_preflight(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Read-only — returns the unmet requirements for promotion."""
    await require_workspace_readable(db, user, workspace_id)
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    try:
        unmet = await preflight_promote(db, workspace_id)
    except PromoteError as exc:
        raise HTTPException(400, str(exc))
    return [
        UnmetRequirementResponse(kind=u.kind, detail=u.detail, payload=u.payload)
        for u in unmet
    ]


@workspace_router.post("/{workspace_id}/promote", response_model=PromoteResponse)
async def promote(
    workspace_id: str,
    req: PromoteRequest = PromoteRequest(),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    if not await user_can_manage_workspace(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        entity_role=user.role,
    ):
        raise HTTPException(403, "You do not have permission to promote this workspace")
    try:
        result = await promote_workspace(
            db, workspace_id, user_id=user.id, force=req.force,
        )
    except PromoteError as exc:
        raise HTTPException(400, str(exc))
    if result.promoted:
        await db.commit()
    return PromoteResponse(
        workspace_id=result.workspace_id,
        promoted=result.promoted,
        unmet=[
            UnmetRequirementResponse(kind=u.kind, detail=u.detail, payload=u.payload)
            for u in result.unmet
        ],
        notes=list(result.notes),
    )
