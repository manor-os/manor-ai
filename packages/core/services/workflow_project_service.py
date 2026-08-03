"""Concurrency-safe persistence for state shared by related workflow runs."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowProject


class WorkflowProjectConflict(RuntimeError):
    """Raised when a caller patches a stale project revision."""


class WorkflowProjectNotFound(LookupError):
    """Raised when a project is absent or outside the caller's entity."""


_UNSET = object()


async def create_workflow_project(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    project_type: str,
    state: dict,
    created_by: str,
    schema_version: int = 1,
    current_stage: str = "draft",
    last_run_id: str | None = None,
) -> WorkflowProject:
    project = WorkflowProject(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        project_type=project_type,
        schema_version=schema_version,
        current_stage=current_stage,
        state=dict(state),
        revision=0,
        last_run_id=last_run_id,
        created_by=created_by,
    )
    db.add(project)
    await db.flush()
    return project


async def get_workflow_project(
    db: AsyncSession,
    *,
    project_id: str,
    entity_id: str,
    workspace_id: str | None = None,
) -> WorkflowProject:
    filters = [
        WorkflowProject.id == project_id,
        WorkflowProject.entity_id == entity_id,
    ]
    if workspace_id is not None:
        filters.append(WorkflowProject.workspace_id == workspace_id)
    project = (await db.execute(
        select(WorkflowProject).where(*filters)
    )).scalar_one_or_none()
    if project is None:
        raise WorkflowProjectNotFound(project_id)
    return project


async def patch_workflow_project(
    db: AsyncSession,
    *,
    project_id: str,
    entity_id: str,
    workspace_id: str | None = None,
    expected_revision: int,
    state: dict,
    current_stage: str | None = None,
    last_run_id: str | None | object = _UNSET,
) -> WorkflowProject:
    values: dict[str, Any] = {
        "state": dict(state),
        "revision": WorkflowProject.revision + 1,
    }
    if current_stage is not None:
        values["current_stage"] = current_stage
    if last_run_id is not _UNSET:
        values["last_run_id"] = last_run_id

    filters = [
        WorkflowProject.id == project_id,
        WorkflowProject.entity_id == entity_id,
        WorkflowProject.revision == expected_revision,
    ]
    if workspace_id is not None:
        filters.append(WorkflowProject.workspace_id == workspace_id)

    updated_id = (await db.execute(
        update(WorkflowProject)
        .where(*filters)
        .values(**values)
        .returning(WorkflowProject.id)
    )).scalar_one_or_none()

    if updated_id is None:
        visibility_filters = [
            WorkflowProject.id == project_id,
            WorkflowProject.entity_id == entity_id,
        ]
        if workspace_id is not None:
            visibility_filters.append(WorkflowProject.workspace_id == workspace_id)
        visible_revision = (await db.execute(
            select(WorkflowProject.revision).where(*visibility_filters)
        )).scalar_one_or_none()
        if visible_revision is None:
            raise WorkflowProjectNotFound(project_id)
        raise WorkflowProjectConflict(
            f"Workflow project {project_id} revision is {visible_revision}, "
            f"expected {expected_revision}"
        )

    project = (await db.execute(
        select(WorkflowProject)
        .where(WorkflowProject.id == updated_id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    return project
