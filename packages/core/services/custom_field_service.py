"""Custom field definition CRUD and validation."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.custom_field import CustomFieldDefinition

VALID_FIELD_TYPES = {
    "text", "number", "date", "select", "multiselect", "boolean", "url", "email", "phone",
}
VALID_TARGETS = {"task", "client", "workspace"}


async def list_field_definitions(
    db: AsyncSession,
    entity_id: str,
    target: str | None = None,
    workspace_id: str | None = None,
) -> list[CustomFieldDefinition]:
    """List custom field definitions for an entity."""
    q = select(CustomFieldDefinition).where(
        (CustomFieldDefinition.entity_id == entity_id)
        & (CustomFieldDefinition.status == "active")
    )
    if target:
        q = q.where(CustomFieldDefinition.target == target)
    if workspace_id:
        q = q.where(
            (CustomFieldDefinition.workspace_id == workspace_id)
            | (CustomFieldDefinition.workspace_id.is_(None))
        )
    q = q.order_by(CustomFieldDefinition.sort_order, CustomFieldDefinition.created_at)
    rows = (await db.execute(q)).scalars().all()
    return list(rows)


async def create_field_definition(
    db: AsyncSession,
    entity_id: str,
    name: str,
    display_name: str,
    field_type: str,
    target: str,
    **kwargs,
) -> CustomFieldDefinition:
    """Create a new custom field definition."""
    fd = CustomFieldDefinition(
        entity_id=entity_id,
        name=name,
        display_name=display_name,
        field_type=field_type,
        target=target,
        **kwargs,
    )
    db.add(fd)
    await db.flush()
    await db.refresh(fd)
    return fd


async def update_field_definition(
    db: AsyncSession,
    field_id: str,
    entity_id: str,
    **kwargs,
) -> CustomFieldDefinition | None:
    """Update an existing custom field definition. Returns None if not found."""
    q = select(CustomFieldDefinition).where(
        (CustomFieldDefinition.id == field_id)
        & (CustomFieldDefinition.entity_id == entity_id)
    )
    fd = (await db.execute(q)).scalar_one_or_none()
    if not fd:
        return None
    for key, value in kwargs.items():
        if hasattr(fd, key):
            setattr(fd, key, value)
    await db.flush()
    await db.refresh(fd)
    return fd


async def delete_field_definition(
    db: AsyncSession,
    field_id: str,
    entity_id: str,
) -> bool:
    """Soft-delete a custom field definition by setting status to 'deleted'."""
    q = select(CustomFieldDefinition).where(
        (CustomFieldDefinition.id == field_id)
        & (CustomFieldDefinition.entity_id == entity_id)
    )
    fd = (await db.execute(q)).scalar_one_or_none()
    if not fd:
        return False
    fd.status = "deleted"
    await db.flush()
    return True


def validate_custom_fields(
    field_defs: list,
    values: dict,
) -> tuple[bool, list[str]]:
    """Validate custom field values against their definitions.

    Returns (is_valid, error_messages).
    Works with both ORM objects and plain dicts.
    """
    errors: list[str] = []
    for fd in field_defs:
        name = fd.name if hasattr(fd, "name") else fd.get("name")
        required = fd.required if hasattr(fd, "required") else fd.get("required", False)
        field_type = fd.field_type if hasattr(fd, "field_type") else fd.get("field_type")

        val = values.get(name)
        if required and (val is None or val == ""):
            errors.append(f"Field '{name}' is required")
            continue
        if val is None:
            continue

        # Type validation
        if field_type == "number" and not isinstance(val, (int, float)):
            try:
                float(val)
            except (ValueError, TypeError):
                errors.append(f"Field '{name}' must be a number")
        elif field_type == "boolean" and not isinstance(val, bool):
            errors.append(f"Field '{name}' must be a boolean")
        elif field_type in ("select", "multiselect"):
            options = fd.options if hasattr(fd, "options") else fd.get("options", [])
            if field_type == "select" and val not in options:
                errors.append(f"Field '{name}' must be one of: {options}")
            elif field_type == "multiselect" and isinstance(val, list):
                invalid = [v for v in val if v not in options]
                if invalid:
                    errors.append(f"Field '{name}' invalid options: {invalid}")

    return len(errors) == 0, errors
