"""Workspace Stats library, definitions, collection, and history endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    get_current_user,
    require_workspace_readable,
    require_workspace_writable,
)
from packages.core.database import get_db
from packages.core.models.document import Integration
from packages.core.models.user import User
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.services.settings_service import update_user_preferences
from packages.core.stats import service as stat_service
from packages.core.stats.integration_keys import (
    StatIntegrationKey,
    provider_aliases_for_stat_integration,
    stat_integration_key_for_provider,
)
from packages.core.stats.library import get_library_entry, list_library_entries


router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

_QUICK_VIEW_PREFERENCE_KEY = "workspace_stats_quick_views"


class StatCreateRequest(BaseModel):
    library_key: Optional[str] = None
    key: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    value_type: str = "number"
    unit: Optional[str] = None
    window: str = "latest"
    collector_type: str = "manual"
    collector_config: dict[str, Any] = Field(default_factory=dict)
    collection_cadence: Optional[str] = None
    freshness_limit_seconds: Optional[int] = Field(default=None, ge=60)
    goal_eligible: bool = True


class StatUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    unit: Optional[str] = None
    window: Optional[str] = None
    collector_config: Optional[dict[str, Any]] = None
    collection_cadence: Optional[str] = None
    freshness_limit_seconds: Optional[int] = Field(default=None, ge=60)
    status: Optional[str] = None
    goal_eligible: Optional[bool] = None


class ObservationCreateRequest(BaseModel):
    value: float
    note: Optional[str] = None
    observed_at: Optional[datetime] = None


class StatQuickViewRequest(BaseModel):
    ordered_stat_ids: list[str] = Field(default_factory=list)
    hidden_stat_ids: list[str] = Field(default_factory=list)


class StatQuickViewResponse(StatQuickViewRequest):
    configured: bool = False


def _freshness_status(stat: WorkspaceStat) -> str:
    if stat.status != "active":
        return stat.status
    if stat.last_collection_status == "error":
        return "collection_error"
    if stat.current_value_updated_at is None:
        return "no_data"
    if stat.freshness_limit_seconds:
        updated = stat.current_value_updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age > stat.freshness_limit_seconds:
            return "stale"
    return "fresh"


def _stat_payload(stat: WorkspaceStat) -> dict[str, Any]:
    return {
        "id": stat.id,
        "workspace_id": stat.workspace_id,
        "key": stat.key,
        "name": stat.name,
        "description": stat.description,
        "value_type": stat.value_type,
        "unit": stat.unit,
        "window": stat.window,
        "collector_type": stat.collector_type,
        "collector_config": stat.collector_config or {},
        "collection_cadence": stat.collection_cadence,
        "freshness_limit_seconds": stat.freshness_limit_seconds,
        "origin": stat.origin,
        "library_key": stat.library_key,
        "status": stat.status,
        "goal_eligible": stat.goal_eligible,
        "current_value": float(stat.current_value) if stat.current_value is not None else None,
        "current_value_updated_at": stat.current_value_updated_at,
        "last_collection_status": stat.last_collection_status,
        "last_collection_error": stat.last_collection_error,
        "freshness_status": _freshness_status(stat),
        "revision": stat.revision,
        "created_at": stat.created_at,
        "updated_at": stat.updated_at,
    }


def _observation_payload(row: WorkspaceStatObservation) -> dict[str, Any]:
    return {
        "id": row.id,
        "stat_id": row.stat_id,
        "value": float(row.value),
        "observed_at": row.observed_at,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "source": row.source,
        "evidence": row.evidence,
        "collector_revision": row.collector_revision,
    }


def _normalize_quick_view(
    value: Any,
    *,
    valid_stat_ids: set[str],
    configured: bool,
) -> StatQuickViewResponse:
    payload = value if isinstance(value, dict) else {}

    def valid_unique(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            stat_id = str(item or "").strip()
            if not stat_id or stat_id not in valid_stat_ids or stat_id in seen:
                continue
            seen.add(stat_id)
            result.append(stat_id)
        return result

    return StatQuickViewResponse(
        ordered_stat_ids=valid_unique(payload.get("ordered_stat_ids")),
        hidden_stat_ids=valid_unique(payload.get("hidden_stat_ids")),
        configured=configured,
    )


def _integration_connection_payload(
    row: Integration, integration_key: StatIntegrationKey,
) -> dict[str, Any]:
    config = row.config or {}
    label = ""
    for key in ("account_name", "display_name", "username", "name", "email"):
        value = str(config.get(key) or "").strip()
        if value:
            label = value
            break
    if (
        integration_key == StatIntegrationKey.TWITTER_X
        and label
        and not label.startswith("@")
    ):
        label = f"@{label}"
    if not label:
        display_key = integration_key.value.replace("_", " ").title()
        label = f"{display_key} · {row.id[-6:]}"
    return {
        "id": row.id,
        "integration_key": integration_key.value,
        "label": label,
        "is_default": bool(config.get("is_default")),
    }


@router.get("/stats/library")
async def stat_library(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = list((await db.execute(
        select(Integration).where(
            Integration.entity_id == user.entity_id,
            Integration.status == "active",
        )
    )).scalars().all())
    connections_by_integration_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        integration_key = stat_integration_key_for_provider(row.provider)
        if integration_key is None:
            continue
        connections_by_integration_key.setdefault(
            integration_key.value, [],
        ).append(_integration_connection_payload(row, integration_key))
    for connections in connections_by_integration_key.values():
        connections.sort(key=lambda item: (
            not item["is_default"], item["label"].casefold(), item["id"],
        ))

    items: list[dict[str, Any]] = []
    for entry in list_library_entries():
        payload = entry.to_dict()
        payload["available_connections"] = []
        if entry.collector_type == "integration":
            integration_key = entry.integration_key
            if integration_key is None:
                continue
            payload["available_connections"] = (
                connections_by_integration_key.get(integration_key.value, [])
            )
            if not payload["available_connections"]:
                continue
        items.append(payload)
    return {"items": items}


@router.get(
    "/{workspace_id}/stats/quick-view",
    response_model=StatQuickViewResponse,
)
async def get_workspace_stats_quick_view(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return this user's compact Stats module layout for one workspace."""
    await require_workspace_readable(db, user, workspace_id)
    rows = await stat_service.list_stats(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    views = (user.preferences or {}).get(_QUICK_VIEW_PREFERENCE_KEY)
    views = views if isinstance(views, dict) else {}
    stored = views.get(workspace_id)
    return _normalize_quick_view(
        stored,
        valid_stat_ids={row.id for row in rows},
        configured=isinstance(stored, dict),
    )


@router.put(
    "/{workspace_id}/stats/quick-view",
    response_model=StatQuickViewResponse,
)
async def put_workspace_stats_quick_view(
    workspace_id: str,
    req: StatQuickViewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist a personal, cross-device compact Stats module layout."""
    await require_workspace_readable(db, user, workspace_id)
    rows = await stat_service.list_stats(
        db,
        entity_id=user.entity_id,
        workspace_id=workspace_id,
    )
    normalized = _normalize_quick_view(
        req.model_dump(),
        valid_stat_ids={row.id for row in rows},
        configured=True,
    )
    preferences = user.preferences or {}
    current_views = preferences.get(_QUICK_VIEW_PREFERENCE_KEY)
    views = dict(current_views) if isinstance(current_views, dict) else {}
    views[workspace_id] = normalized.model_dump(exclude={"configured"})
    await update_user_preferences(
        db,
        user.id,
        {_QUICK_VIEW_PREFERENCE_KEY: views},
    )
    return normalized


@router.get("/{workspace_id}/stats")
async def list_workspace_stats(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_readable(db, user, workspace_id)
    rows = await stat_service.list_stats(db, entity_id=user.entity_id, workspace_id=workspace_id)
    return {"items": [_stat_payload(row) for row in rows]}


@router.post("/{workspace_id}/stats", status_code=201)
async def create_workspace_stat(
    workspace_id: str,
    req: StatCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_writable(db, user, workspace_id)
    try:
        if req.library_key:
            entry = get_library_entry(req.library_key)
            if entry is None:
                raise stat_service.StatError(
                    f"unknown stat library key: {req.library_key}"
                )
            collector_overrides = dict(req.collector_config or {})
            if entry.collector_type == "integration":
                integration_key = entry.integration_key
                if integration_key is None:
                    raise stat_service.StatError(
                        "integration library stat is missing integration_key"
                    )
                connection_id = str(
                    collector_overrides.get("connection_id") or ""
                ).strip()
                if not connection_id:
                    raise stat_service.StatError(
                        f"select an active {integration_key.value} connection"
                    )
                connection = (await db.execute(
                    select(Integration).where(
                        Integration.id == connection_id,
                        Integration.entity_id == user.entity_id,
                        Integration.provider.in_(
                            provider_aliases_for_stat_integration(integration_key)
                        ),
                        Integration.status == "active",
                    )
                )).scalar_one_or_none()
                if connection is None:
                    raise stat_service.StatError(
                        f"the selected {integration_key.value} connection is not active"
                    )
                collector_overrides["connection_id"] = connection.id
            cadence_override = (
                {"collection_cadence": req.collection_cadence}
                if "collection_cadence" in req.model_fields_set else {}
            )
            row = await stat_service.create_stat_from_library(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                library_key=req.library_key,
                key=req.key,
                name=req.name,
                window=req.window if "window" in req.model_fields_set else None,
                collector_overrides=collector_overrides,
                origin="library",
                **cadence_override,
            )
        else:
            if not req.key or not req.name:
                raise stat_service.StatError("custom stats require key and name")
            row = await stat_service.create_stat(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                key=req.key,
                name=req.name,
                description=req.description,
                value_type=req.value_type,
                unit=req.unit,
                window=req.window,
                collector_type=req.collector_type,
                collector_config=req.collector_config,
                collection_cadence=req.collection_cadence,
                freshness_limit_seconds=req.freshness_limit_seconds,
                origin="user",
                goal_eligible=req.goal_eligible,
            )
        await db.commit()
        await db.refresh(row)
        return _stat_payload(row)
    except stat_service.StatError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc


async def _writable_stat(
    db: AsyncSession, user: User, workspace_id: str, stat_id: str,
) -> WorkspaceStat:
    await require_workspace_writable(db, user, workspace_id)
    row = await stat_service.get_stat(db, stat_id, user.entity_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, "stat not found")
    return row


@router.patch("/{workspace_id}/stats/{stat_id}")
async def update_workspace_stat(
    workspace_id: str,
    stat_id: str,
    req: StatUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _writable_stat(db, user, workspace_id, stat_id)
    fields = req.model_dump(exclude_unset=True)
    if fields.get("status") not in {None, "active", "paused"}:
        raise HTTPException(400, "status must be active or paused")
    try:
        await stat_service.update_stat(db, row, **fields)
        await db.commit()
        await db.refresh(row)
        return _stat_payload(row)
    except stat_service.StatError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{workspace_id}/stats/{stat_id}", status_code=204)
async def delete_workspace_stat(
    workspace_id: str,
    stat_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _writable_stat(db, user, workspace_id, stat_id)
    await stat_service.delete_stat(db, row)
    await db.commit()


@router.post("/{workspace_id}/stats/{stat_id}/collect")
async def collect_workspace_stat(
    workspace_id: str,
    stat_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _writable_stat(db, user, workspace_id, stat_id)
    try:
        observation = await stat_service.collect_stat(db, row)
        await db.commit()
        await db.refresh(row)
        return {"stat": _stat_payload(row), "observation": _observation_payload(observation)}
    except stat_service.StatError as exc:
        await db.commit()  # preserve the visible collection_error state
        raise HTTPException(400, str(exc)) from exc


@router.get("/{workspace_id}/stats/{stat_id}/observations")
async def list_workspace_stat_observations(
    workspace_id: str,
    stat_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_workspace_readable(db, user, workspace_id)
    row = await stat_service.get_stat(db, stat_id, user.entity_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, "stat not found")
    observations = await stat_service.list_observations(db, stat_id=stat_id, limit=limit)
    return {"items": [_observation_payload(item) for item in observations]}


@router.post("/{workspace_id}/stats/{stat_id}/observations", status_code=201)
async def record_workspace_stat_observation(
    workspace_id: str,
    stat_id: str,
    req: ObservationCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _writable_stat(db, user, workspace_id, stat_id)
    observation = await stat_service.record_observation(
        db,
        row,
        value=req.value,
        source="manual",
        observed_at=req.observed_at,
        evidence={"note": req.note} if req.note else None,
    )
    await db.commit()
    await db.refresh(row)
    return {"stat": _stat_payload(row), "observation": _observation_payload(observation)}
