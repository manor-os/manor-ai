"""Custom field definition endpoints — CRUD for extensible entity fields."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.custom_field_service import (
    list_field_definitions,
    create_field_definition,
    update_field_definition,
    delete_field_definition,
    VALID_FIELD_TYPES,
    VALID_TARGETS,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/custom-fields", tags=["custom-fields"])


class FieldDefinitionResponse(BaseModel):
    id: str
    entity_id: str
    workspace_id: str | None = None
    name: str
    display_name: str
    field_type: str
    target: str
    options: list = []
    default_value: str | None = None
    required: bool = False
    sort_order: int = 0
    status: str = "active"


class FieldDefinitionCreateRequest(BaseModel):
    name: str
    display_name: str
    field_type: str
    target: str
    workspace_id: str | None = None
    options: list = []
    default_value: str | None = None
    required: bool = False
    sort_order: int = 0


class FieldDefinitionUpdateRequest(BaseModel):
    display_name: str | None = None
    field_type: str | None = None
    options: list | None = None
    default_value: str | None = None
    required: bool | None = None
    sort_order: int | None = None


def _to_response(fd) -> FieldDefinitionResponse:
    return FieldDefinitionResponse(
        id=fd.id,
        entity_id=fd.entity_id,
        workspace_id=fd.workspace_id,
        name=fd.name,
        display_name=fd.display_name,
        field_type=fd.field_type,
        target=fd.target,
        options=fd.options or [],
        default_value=fd.default_value,
        required=fd.required,
        sort_order=fd.sort_order,
        status=fd.status,
    )


@router.get("", response_model=list[FieldDefinitionResponse])
async def list_custom_fields(
    target: str | None = None,
    workspace_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List custom field definitions, optionally filtered by target and workspace."""
    fields = await list_field_definitions(db, user.entity_id, target=target, workspace_id=workspace_id)
    return [_to_response(f) for f in fields]


@router.post("", response_model=FieldDefinitionResponse, status_code=201)
async def create_custom_field(
    req: FieldDefinitionCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new custom field definition."""
    if req.field_type not in VALID_FIELD_TYPES:
        raise HTTPException(400, f"Invalid field_type. Must be one of: {sorted(VALID_FIELD_TYPES)}")
    if req.target not in VALID_TARGETS:
        raise HTTPException(400, f"Invalid target. Must be one of: {sorted(VALID_TARGETS)}")
    fd = await create_field_definition(
        db,
        entity_id=user.entity_id,
        name=req.name,
        display_name=req.display_name,
        field_type=req.field_type,
        target=req.target,
        workspace_id=req.workspace_id,
        options=req.options,
        default_value=req.default_value,
        required=req.required,
        sort_order=req.sort_order,
    )
    return _to_response(fd)


@router.put("/{field_id}", response_model=FieldDefinitionResponse)
async def update_custom_field(
    field_id: str,
    req: FieldDefinitionUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing custom field definition."""
    updates = req.model_dump(exclude_none=True)
    if "field_type" in updates and updates["field_type"] not in VALID_FIELD_TYPES:
        raise HTTPException(400, f"Invalid field_type. Must be one of: {sorted(VALID_FIELD_TYPES)}")
    fd = await update_field_definition(db, field_id, user.entity_id, **updates)
    if not fd:
        raise HTTPException(404, "Custom field not found")
    return _to_response(fd)


@router.delete("/{field_id}", status_code=204)
async def delete_custom_field(
    field_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a custom field definition."""
    ok = await delete_field_definition(db, field_id, user.entity_id)
    if not ok:
        raise HTTPException(404, "Custom field not found")
