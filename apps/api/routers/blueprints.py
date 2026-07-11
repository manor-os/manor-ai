"""Workspace Blueprint endpoints — export / list / install / promote.

The list/detail/install endpoints serve both entity-owned blueprints and
code-shipped marketplace blueprints. Entity-owned blueprints live in
``workspace_blueprints``; built-ins are frozen JSON configs addressed as
``builtin:<slug>``.

Endpoints:

  POST   /api/v1/workspaces/{id}/export-blueprint   export current ws as draft
  GET    /api/v1/blueprints                          list mine
  GET    /api/v1/blueprints/{id}                     fetch one (mine or published)
  PUT    /api/v1/blueprints/{id}                     edit metadata
  DELETE /api/v1/blueprints/{id}                     delete
  POST   /api/v1/blueprints/{id}/install             install (mode=simulate|live)
  POST   /api/v1/workspaces/{id}/promote             sandbox → live
  POST   /api/v1/workspaces/{id}/promote/preflight   read-only check
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.blueprints import (
    ExportError,
    InstallError,
    InstallMode,
    InstallResult,
    PromoteError,
    SOLO_COMPANY_BLUEPRINTS_FROZEN_AT,
    SimulationReport,
    export_workspace,
    get_solo_company_blueprint,
    get_solo_company_blueprints,
    install_blueprint,
    preflight_promote,
    promote_workspace,
    simulate_report,
)
from packages.core.blueprints.exporter import ExportContext
from packages.core.governance.presets import list_presets
from packages.core.database import get_db
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.user import User
from packages.core.services.entity_service import get_workspace
from packages.core.services.merchant_service import get_merchant_account

# Two routers because we need two prefixes (one workspace-scoped, one
# blueprint-scoped). Both registered in main.py.
blueprint_router = APIRouter(prefix="/api/v1/blueprints", tags=["blueprints"])
workspace_router = APIRouter(prefix="/api/v1/workspaces", tags=["blueprints"])

_BUILTIN_BLUEPRINT_PREFIX = "builtin:"
_BUILTIN_CREATED_AT = datetime.fromisoformat(
    f"{SOLO_COMPANY_BLUEPRINTS_FROZEN_AT}T12:00:00+00:00"
).astimezone(timezone.utc)
BLUEPRINT_STATUS_DRAFT = "draft"
BLUEPRINT_STATUS_PENDING_REVIEW = "pending_review"
BLUEPRINT_STATUS_PUBLISHED = "published"
BLUEPRINT_STATUS_ARCHIVED = "archived"


# ── Models ────────────────────────────────────────────────────────────

class ExportBlueprintRequest(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{1,118}[a-z0-9]$")
    title: str = Field(..., min_length=1, max_length=200)
    summary: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    cover_image_url: Optional[str] = None
    author_handle: Optional[str] = None
    author_display_name: Optional[str] = None
    # Section toggles — pass overrides only if you want to drop something
    include_subscriptions: bool = True
    include_goals: bool = True
    include_scheduled_jobs: bool = True
    include_custom_fields: bool = True
    include_governance: bool = True
    include_channel_requirements: bool = True
    include_session_requirements: bool = True
    include_memory_files: bool = False


class BlueprintSetupItem(BaseModel):
    label: str
    key: Optional[str] = None
    kind: Optional[str] = None
    required: bool = False
    purpose: Optional[str] = None
    default: Optional[str] = None


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
    first_week_outputs: list[str] = Field(default_factory=list)
    validation_evidence: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    not_included: list[str] = Field(default_factory=list)
    services: list[BlueprintSetupItem] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)


class BlueprintSummary(BaseModel):
    id: str
    slug: str
    title: str
    summary: Optional[str] = None
    tags: list[str]
    status: str
    install_count: int
    payload_version: str
    source_workspace_id: Optional[str] = None
    cover_image_url: Optional[str] = None
    setup_preview: BlueprintSetupPreview = Field(default_factory=BlueprintSetupPreview)
    created_at: datetime
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    price_cents: Optional[int] = None
    currency: str = "usd"
    purchase_count: int = 0
    has_share_token: bool = False
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


class UpdateBlueprintRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    summary: Optional[str] = Field(None, max_length=500)
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    cover_image_url: Optional[str] = None


class SubmitBlueprintReviewRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=1000)


class BlueprintPricingRequest(BaseModel):
    price_cents: int = Field(ge=0, le=1_000_000)


class ShareTokenResponse(BaseModel):
    share_token: str


class InstallBlueprintRequest(BaseModel):
    mode: InstallMode = InstallMode.SIMULATE
    workspace_name: Optional[str] = Field(None, max_length=200)
    share_token: Optional[str] = None
    create_missing_agents: bool = False
    governance_preset: str = Field(
        "standard",
        pattern="^(safe|standard|aggressive)$",
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
    goal_ids: list[str]
    subscription_ids: list[str]
    scheduled_job_ids: list[str]
    custom_field_ids: list[str]
    governance_applied: bool
    todos: list[InstallTodoResponse]
    notes: list[str]


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
    activity: ActivityResponse
    cost: CostResponse
    counterfactuals: list[CounterfactualResponse]
    goals: list[GoalPaceResponse]
    notes: list[str]


class PromoteRequest(BaseModel):
    force: bool = False


class PromoteResponse(BaseModel):
    workspace_id: str
    promoted: bool
    unmet: list[UnmetRequirementResponse]
    notes: list[str]


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
    default = row.get("default")
    return BlueprintSetupItem(
        label=label,
        key=_string_or_none(row.get("key")),
        kind="variable",
        required=bool(row.get("required", False)),
        default=str(default) if default is not None else None,
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
    expected = _as_record(policy.get("expected_baseline"))

    variables = [
        item for item in (
            _setup_item_from_variable(raw)
            for raw in _as_list(contract.get("variables"))
        )
        if item is not None
    ]
    channels = [
        item for item in (
            _setup_item_from_channel(raw)
            for raw in (_as_list(contract.get("channels")) or _as_list(p.get("channel_requirements")))
        )
        if item is not None
    ]
    sessions = [
        item for item in (
            _setup_item_from_session(raw)
            for raw in (_as_list(contract.get("sessions")) or _as_list(p.get("session_requirements")))
        )
        if item is not None
    ]
    services = [
        item for item in (
            _setup_item_from_service(raw)
            for raw in _as_list(operating_model.get("services"))
        )
        if item is not None
    ]
    required_variables, optional_variables = _split_required(variables)
    required_channels, optional_channels = _split_required(channels)
    required_sessions, optional_sessions = _split_required(sessions)
    blocking = expected.get("blocking_todos_expected")

    return BlueprintSetupPreview(
        use_when=_string_or_none(manifest.get("use_when")),
        maturity_level=(
            _string_or_none(expected.get("maturity_level"))
            or _string_or_none(manifest.get("maturity_level"))
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
        first_week_outputs=_string_list(expected.get("first_week_outputs")),
        validation_evidence=_string_list(expected.get("validation_evidence")),
        acceptance_criteria=_string_list(expected.get("acceptance_criteria")),
        not_included=_string_list(expected.get("not_included")),
        services=services,
        rules=_string_list(operating_model.get("rules")),
    )


def _summary(b: WorkspaceBlueprint, viewer_entity_id: str) -> BlueprintSummary:
    is_owner = b.entity_id == viewer_entity_id
    return BlueprintSummary(
        id=b.id,
        slug=b.slug,
        title=b.title,
        summary=b.summary,
        tags=list(b.tags or []),
        status=b.status,
        install_count=b.install_count,
        payload_version=b.payload_version,
        source_workspace_id=b.source_workspace_id,
        cover_image_url=b.cover_image_url,
        setup_preview=_payload_setup_preview(b.payload),
        created_at=b.created_at,
        updated_at=b.updated_at,
        published_at=b.published_at,
        price_cents=b.price_cents,
        currency=b.currency,
        purchase_count=b.purchase_count,
        # share-token existence is owner-only metadata.
        has_share_token=bool(b.share_token) if is_owner else False,
        is_owner=is_owner,
    )


def _manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest")
    return manifest if isinstance(manifest, dict) else {}


def _builtin_id(slug: str) -> str:
    return f"{_BUILTIN_BLUEPRINT_PREFIX}{slug}"


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


def _builtin_summary(payload: dict[str, Any]) -> BlueprintSummary:
    manifest = _manifest(payload)
    slug = str(manifest.get("slug") or "")
    raw_tags = manifest.get("tags")
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
    cover_image_url = manifest.get("cover_image_url")
    return BlueprintSummary(
        id=_builtin_id(slug),
        slug=slug,
        title=str(manifest.get("title") or slug),
        summary=(
            str(manifest["summary"])
            if isinstance(manifest.get("summary"), str)
            else None
        ),
        tags=tags,
        status=BLUEPRINT_STATUS_PUBLISHED,
        install_count=0,
        payload_version=str(manifest.get("blueprint_version") or "1.1"),
        source_workspace_id=None,
        cover_image_url=(
            str(cover_image_url)
            if isinstance(cover_image_url, str) and cover_image_url
            else None
        ),
        setup_preview=_payload_setup_preview(payload),
        created_at=_BUILTIN_CREATED_AT,
        updated_at=_BUILTIN_CREATED_AT,
        published_at=_BUILTIN_CREATED_AT,
        price_cents=None,
        currency="usd",
        purchase_count=0,
        has_share_token=False,
        is_owner=False,
    )


def _builtin_detail(payload: dict[str, Any]) -> BlueprintDetail:
    manifest = _manifest(payload)
    description = manifest.get("description")
    return BlueprintDetail(
        **_builtin_summary(payload).model_dump(),
        description=str(description) if isinstance(description, str) else None,
        payload=payload,
    )


async def _has_completed_purchase(
    db: AsyncSession, blueprint_id: str, entity_id: str,
) -> bool:
    """True when ``entity_id`` holds a live (completed) entitlement."""
    from packages.core.models.blueprint_purchase import BlueprintPurchase
    return (await db.execute(
        select(BlueprintPurchase.id).where(
            BlueprintPurchase.blueprint_id == blueprint_id,
            BlueprintPurchase.buyer_entity_id == entity_id,
            BlueprintPurchase.status == "completed",
        )
    )).scalar_one_or_none() is not None


async def _load_blueprint(
    db: AsyncSession, blueprint_id: str, entity_id: str,
    *, allow_published: bool = True, share_token: Optional[str] = None,
) -> WorkspaceBlueprint:
    """Loader with tenant + visibility check. Owners always see their
    own; anyone authenticated sees published ones. A valid share token
    unlocks any status (unlisted distribution), and a completed purchase
    keeps the blueprint visible to its buyer even after the seller
    archives/unpublishes it (spec §4.3/§5.3).

    The token/purchase grants apply only to ``allow_published=True`` loads:
    ``allow_published=False`` marks owner-mutation contexts (update, delete,
    pricing, share-token), where non-owners must always 404."""
    row = (await db.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.id == blueprint_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "blueprint not found")
    if row.entity_id != entity_id:
        if not (allow_published and row.status == BLUEPRINT_STATUS_PUBLISHED):
            token_ok = allow_published and bool(share_token) and bool(
                row.share_token
            ) and share_token == row.share_token
            purchased_ok = False
            if not token_ok and allow_published:
                purchased_ok = await _has_completed_purchase(db, row.id, entity_id)
            if not token_ok and not purchased_ok:
                raise HTTPException(404, "blueprint not found")
    return row


def _install_response(r: InstallResult) -> InstallResponse:
    return InstallResponse(
        workspace_id=r.workspace_id,
        mode=r.mode.value,
        blueprint_id=r.blueprint_id,
        blueprint_slug=r.blueprint_slug,
        goal_ids=list(r.goal_ids),
        subscription_ids=list(r.subscription_ids),
        scheduled_job_ids=list(r.scheduled_job_ids),
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


# ── Export (workspace → draft blueprint row) ──────────────────────────

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
    Operator can then edit metadata + publish via PUT/POST publish."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")

    # Slug uniqueness — surface as 409 with a useful message.
    existing = (await db.execute(
        select(WorkspaceBlueprint).where(
            WorkspaceBlueprint.entity_id == user.entity_id,
            WorkspaceBlueprint.slug == req.slug,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"blueprint slug {req.slug!r} already used")

    ctx = ExportContext(
        include_subscriptions=req.include_subscriptions,
        include_goals=req.include_goals,
        include_scheduled_jobs=req.include_scheduled_jobs,
        include_custom_fields=req.include_custom_fields,
        include_governance=req.include_governance,
        include_channel_requirements=req.include_channel_requirements,
        include_session_requirements=req.include_session_requirements,
        include_memory_files=req.include_memory_files,
    )
    try:
        payload = await export_workspace(
            db, workspace_id,
            title=req.title,
            summary=req.summary,
            description=req.description,
            tags=req.tags,
            author_handle=req.author_handle,
            author_display_name=req.author_display_name,
            context=ctx,
        )
    except ExportError as exc:
        raise HTTPException(400, str(exc))

    row = WorkspaceBlueprint(
        entity_id=user.entity_id,
        slug=req.slug,
        source_workspace_id=workspace_id,
        title=req.title,
        summary=req.summary,
        description=req.description,
        cover_image_url=req.cover_image_url,
        tags=list(req.tags),
        payload=payload,
        # v1.1 nests the version under manifest; v1.0 had it top-level.
        # Read both shapes so older stored payloads keep working.
        payload_version=(
            (payload.get("manifest") or {}).get("blueprint_version")
            or payload.get("blueprint_version")
        ),
        status=BLUEPRINT_STATUS_DRAFT,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    detail = BlueprintDetail(
        **_summary(row, user.entity_id).model_dump(),
        description=row.description,
        payload=row.payload,
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
    if status == BLUEPRINT_STATUS_PUBLISHED:
        visibility = WorkspaceBlueprint.status == BLUEPRINT_STATUS_PUBLISHED
    elif status:
        visibility = (
            (WorkspaceBlueprint.entity_id == user.entity_id)
            & (WorkspaceBlueprint.status == status)
        )
    else:
        visibility = (
            (WorkspaceBlueprint.entity_id == user.entity_id)
            | (WorkspaceBlueprint.status == BLUEPRINT_STATUS_PUBLISHED)
        )
    stmt = select(WorkspaceBlueprint).where(visibility).order_by(
        WorkspaceBlueprint.updated_at.desc().nulls_last(),
        WorkspaceBlueprint.created_at.desc(),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    builtins = []
    if status in (None, BLUEPRINT_STATUS_PUBLISHED):
        builtins = [
            _builtin_summary(payload)
            for payload in get_solo_company_blueprints()
        ]
    summaries = [_summary(r, user.entity_id) for r in rows]
    return [*builtins, *summaries]


# Literal-path routes MUST come before the parameterised /{blueprint_id}
# route — otherwise FastAPI matches "governance-presets" as a blueprint id.
@blueprint_router.get(
    "/governance-presets",
    response_model=list[GovernancePresetSummary],
)
async def get_governance_presets(
    user: User = Depends(get_current_user),
):
    """The 3 install-time governance overlays the operator can pick from."""
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
    purchased = False
    if row.entity_id != user.entity_id:
        purchased = await _has_completed_purchase(db, row.id, user.entity_id)
    # The payload IS the paid product — a share token grants VIEWING only.
    # Hide it from non-purchasers; summary/setup_preview stay for the buy page.
    payload = row.payload
    if (row.price_cents or 0) > 0 and row.entity_id != user.entity_id and not purchased:
        payload = {}
    return BlueprintDetail(
        **_summary(row, user.entity_id).model_dump(),
        description=row.description,
        payload=payload,
        purchased=purchased,
    )


@blueprint_router.get("/{blueprint_id}", response_model=BlueprintDetail)
async def get_blueprint(
    blueprint_id: str,
    share_token: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    builtin = _builtin_payload_for_id(blueprint_id)
    if builtin is not None:
        _, payload = builtin
        return _builtin_detail(payload)

    row = await _load_blueprint(
        db, blueprint_id, user.entity_id, share_token=share_token,
    )
    purchased = False
    if row.entity_id != user.entity_id:
        purchased = await _has_completed_purchase(db, row.id, user.entity_id)
    # The payload IS the paid product — hide it from non-purchasers.
    # summary/setup_preview stay intact for the buy page.
    payload = row.payload
    if (row.price_cents or 0) > 0 and row.entity_id != user.entity_id and not purchased:
        payload = {}
    detail = BlueprintDetail(
        **_summary(row, user.entity_id).model_dump(),
        description=row.description,
        payload=payload,
        purchased=purchased,
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
    if req.title is not None:
        row.title = req.title
    if req.summary is not None:
        row.summary = req.summary
    if req.description is not None:
        row.description = req.description
    if req.tags is not None:
        row.tags = list(req.tags)
    if req.cover_image_url is not None:
        row.cover_image_url = req.cover_image_url
    if row.status == BLUEPRINT_STATUS_PUBLISHED:
        row.status = BLUEPRINT_STATUS_PENDING_REVIEW
        row.published_at = None
    await db.flush()
    await db.refresh(row)
    summary = _summary(row, user.entity_id)
    await db.commit()
    return summary


@blueprint_router.put("/{blueprint_id}/pricing", response_model=BlueprintSummary)
async def set_blueprint_pricing(
    blueprint_id: str,
    req: BlueprintPricingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set the marketplace price. Free (0) works everywhere; a paid price
    requires cloud mode and a charges-enabled merchant account. Does NOT
    touch review status — content review and pricing are orthogonal."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be priced")

    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)

    if req.price_cents > 0:
        if os.getenv("DEPLOYMENT_MODE", "oss") != "cloud":
            raise HTTPException(403, "Paid blueprints are only available in cloud mode")
        merchant = await get_merchant_account(db, user.entity_id)
        if merchant is None or not merchant.charges_enabled:
            raise HTTPException(
                409,
                "Connect a payout account before setting a price "
                "(POST /api/v1/merchant/onboard)",
            )

    row.price_cents = req.price_cents
    await db.flush()
    await db.refresh(row)
    summary = _summary(row, user.entity_id)
    await db.commit()
    return summary


@blueprint_router.post("/{blueprint_id}/share-token", response_model=ShareTokenResponse)
async def create_or_rotate_share_token(
    blueprint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or rotate the blueprint's share token (owner only)."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in blueprints cannot be link-shared")
    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
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
    await db.commit()


@blueprint_router.post("/{blueprint_id}/submit-review", response_model=BlueprintSummary)
async def submit_blueprint_for_review(
    blueprint_id: str,
    req: SubmitBlueprintReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit an owned blueprint for platform-admin marketplace review."""
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints are already published")

    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    if row.status == BLUEPRINT_STATUS_PUBLISHED:
        raise HTTPException(409, "blueprint is already published")
    if row.status == BLUEPRINT_STATUS_PENDING_REVIEW:
        return _summary(row, user.entity_id)
    if row.status not in (BLUEPRINT_STATUS_DRAFT, BLUEPRINT_STATUS_ARCHIVED):
        raise HTTPException(409, f"blueprint cannot be submitted from status {row.status!r}")

    row.status = BLUEPRINT_STATUS_PENDING_REVIEW
    row.published_at = None
    # ``note`` is intentionally not persisted in the portable payload. The
    # review workflow is status-based; admin approve/reject records the audit
    # reason separately so installs continue to consume a clean blueprint JSON.
    await db.flush()
    await db.refresh(row)
    summary = _summary(row, user.entity_id)
    await db.commit()
    return summary


@blueprint_router.delete("/{blueprint_id}", status_code=204)
async def delete_blueprint(
    blueprint_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if _builtin_payload_for_id(blueprint_id) is not None:
        raise HTTPException(409, "built-in marketplace blueprints cannot be deleted")

    row = await _load_blueprint(db, blueprint_id, user.entity_id, allow_published=False)
    await db.delete(row)
    await db.commit()


# ── Install ───────────────────────────────────────────────────────────

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
    builtin = _builtin_payload_for_id(blueprint_id)
    if builtin is not None:
        slug, payload = builtin
        try:
            result = await install_blueprint(
                db,
                entity_id=user.entity_id,
                payload=payload,
                mode=req.mode,
                workspace_name=req.workspace_name,
                user_id=user.id,
                blueprint_slug=slug,
                create_missing_agents=req.create_missing_agents,
                governance_preset=req.governance_preset,
            )
        except InstallError as exc:
            raise HTTPException(400, str(exc))

        await db.commit()
        return _install_response(result)

    row = await _load_blueprint(
        db, blueprint_id, user.entity_id, share_token=req.share_token,
    )
    payload = row.payload
    if (row.price_cents or 0) > 0 and row.entity_id != user.entity_id:
        from packages.core.models.blueprint_purchase import BlueprintPurchase
        purchase = (await db.execute(
            select(BlueprintPurchase).where(
                BlueprintPurchase.blueprint_id == row.id,
                BlueprintPurchase.buyer_entity_id == user.entity_id,
                BlueprintPurchase.status == "completed",
            )
        )).scalar_one_or_none()
        if purchase is None:
            raise HTTPException(402, "purchase required to install this blueprint")
        # Install the snapshot the buyer paid for — immune to later
        # seller edits of the live row.
        payload = purchase.payload_snapshot
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
            create_missing_agents=req.create_missing_agents,
            governance_preset=req.governance_preset,
        )
    except InstallError as exc:
        raise HTTPException(400, str(exc))

    row.install_count += 1
    await db.commit()
    return _install_response(result)


# ── Install from raw payload (no DB row required) ─────────────────────
# Useful for previewing a freshly-exported payload OR for tooling that
# generates blueprints externally without round-tripping through the
# blueprint table.
#
# Paid-content note: this endpoint installs CALLER-SUPPLIED payloads only.
# It grants nothing the caller doesn't already possess — the detail/resolve
# endpoints hide paid payloads from non-purchasers, so a non-purchaser can
# never obtain a paid payload through the API to feed in here.

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
    governance_preset: str = Body("standard"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await install_blueprint(
            db,
            entity_id=user.entity_id,
            payload=payload,
            mode=mode,
            workspace_name=workspace_name,
            user_id=user.id,
            create_missing_agents=create_missing_agents,
            governance_preset=governance_preset,
        )
    except InstallError as exc:
        raise HTTPException(400, str(exc))
    await db.commit()
    return _install_response(result)


# ── Simulation report (M12.4) ─────────────────────────────────────────

@workspace_router.get(
    "/{workspace_id}/simulation-report",
    response_model=SimulationReportResponse,
)
async def get_simulation_report(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activity + cost + counterfactual + goal-pace digest of what the
    workspace did during simulation. Read-only — safe to call repeatedly
    while the operator is deciding whether to promote."""
    ws = await get_workspace(db, workspace_id, user.entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    try:
        report = await simulate_report(db, workspace_id)
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
