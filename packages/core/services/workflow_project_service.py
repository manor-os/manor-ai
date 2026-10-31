"""Concurrency-safe persistence for state shared by related workflow runs."""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from copy import deepcopy
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowProject, WorkflowRun
from packages.core.services.project_references import preserve_pinned_references
from packages.core.services.workspace_layout import PROJECT_ARTIFACT_SEGMENT


class WorkflowProjectConflict(RuntimeError):
    """Raised when a caller patches a stale project revision."""


class WorkflowProjectNotFound(LookupError):
    """Raised when a project is absent or outside the caller's entity."""


_UNSET = object()


def _state_value(state: dict, path: str) -> Any:
    value: Any = state
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _set_state_value(state: dict, path: str, value: Any) -> None:
    target = state
    parts = path.split(".")
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


def project_folder_name(name: Any, fallback: str) -> str:
    """A readable folder name for a project: its title, in any script."""
    text = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    slug = re.sub(r"[^\w]+", "-", text).strip("-_")[:60].strip("-_")
    return slug or fallback


async def unique_project_root(
    db: AsyncSession, *, entity_id: str, workspace_id: str, name: Any, under: Any = None, fallback: str = "project",
) -> str:
    """The folder a new project keeps its files in: ``projects/<name>``, or under its parent's.

    ``under`` is a parent project's folder (so a series holds its episodes);
    the name gets ``-2``, ``-3`` ... when another project in the Workspace
    already uses it.
    """
    parent = str(under or PROJECT_ARTIFACT_SEGMENT).strip().strip("/")
    parts = parent.split("/")
    if parts[0] != PROJECT_ARTIFACT_SEGMENT or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"A project folder must be inside {PROJECT_ARTIFACT_SEGMENT}/")
    base = f"{parent}/{project_folder_name(name, fallback)}"
    root = WorkflowProject.state["project_root"].astext
    taken = set((await db.execute(select(root).where(
        WorkflowProject.entity_id == entity_id, WorkflowProject.workspace_id == workspace_id,
        root.startswith(base, autoescape=True),
    ))).scalars())
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


async def list_workflow_projects(
    db: AsyncSession, *, entity_id: str, workspace_id: str, project_type: str,
    stages: set[str] | None = None, parent_id: str | None = None,
    parent_state_key: str = "parent_project_id", limit: int = 100, after_id: str | None = None,
) -> tuple[list[WorkflowProject], str | None]:
    """Bounded Workspace-local project lookup for declarative workflows."""
    if not project_type or not 1 <= limit <= 100:
        raise ValueError("project_type and a limit from 1 to 100 are required")
    filters = [
        WorkflowProject.entity_id == entity_id, WorkflowProject.workspace_id == workspace_id,
        WorkflowProject.project_type == project_type,
    ]
    if stages:
        filters.append(WorkflowProject.current_stage.in_(stages))
    if parent_id is not None:
        filters.append(WorkflowProject.state[parent_state_key].astext == parent_id)
    if after_id:
        filters.append(WorkflowProject.id > after_id)
    rows = list((await db.execute(select(WorkflowProject).where(*filters)
        .order_by(WorkflowProject.id).limit(limit + 1))).scalars())
    return rows[:limit], (rows[limit - 1].id if len(rows) > limit else None)


async def lease_due_workflow_project(
    db: AsyncSession, *, entity_id: str, workspace_id: str, project_type: str,
    run_id: str, parent_id: str | None = None, parent_state_key: str = "parent_project_id",
    due_path: str = "due_at", lease_path: str = "lease", pending_stages: set[str] | None = None,
    lease_seconds: int = 900, recovery: str = "reclaim", limit: int = 100,
    now: datetime | None = None,
) -> WorkflowProject | None:
    if (recovery not in {"reclaim", "verification_required"}
            or not 1 <= lease_seconds <= 86400 or not 1 <= limit <= 100
            or not project_type or not run_id):
        raise ValueError("invalid lease configuration")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("lease time must include a timezone")
    now = now.astimezone(UTC)
    pending_stages = set(pending_stages or {"pending"})
    filters = [
        WorkflowProject.entity_id == entity_id,
        WorkflowProject.workspace_id == workspace_id,
        WorkflowProject.project_type == project_type,
        WorkflowProject.current_stage.in_(pending_stages | {"leased"}),
    ]
    if parent_id is not None:
        filters.append(WorkflowProject.state[parent_state_key].astext == parent_id)

    def is_due(state: dict) -> bool:
        try:
            due_at = datetime.fromisoformat(str(_state_value(state, due_path)))
            return due_at.tzinfo is not None and due_at.astimezone(UTC) <= now
        except (TypeError, ValueError):
            return False

    # Paginate before locking: a batch of future or active items must not starve
    # a due item later in the queue. IDs remain stable when a lease updates it.
    after_id = ""
    while True:
        candidates = (await db.execute(select(WorkflowProject.id, WorkflowProject.state)
            .where(*filters, WorkflowProject.id > after_id)
            .order_by(WorkflowProject.id).limit(limit))).all()
        if not candidates:
            return None
        after_id = candidates[-1].id
        for candidate in candidates:
            if not is_due(candidate.state or {}):
                continue
            row = await db.scalar(select(WorkflowProject)
                .where(*filters, WorkflowProject.id == candidate.id)
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if row is None or not is_due(row.state or {}):
                continue
            state = deepcopy(row.state or {})
            lease = _state_value(state, lease_path) or {}
            pending_stage = row.current_stage
            if row.current_stage == "leased":
                if (not isinstance(lease, dict) or lease.get("due_path") != due_path
                        or lease.get("pending_stage") not in pending_stages):
                    continue
                pending_stage = lease["pending_stage"]
                try:
                    expires = datetime.fromisoformat(str(lease.get("expires_at")))
                    expired = expires.tzinfo is None or expires.astimezone(UTC) <= now
                except ValueError:
                    expired = True
                if not expired:
                    continue
                active_run = await db.scalar(select(WorkflowRun.id).where(
                    WorkflowRun.id == lease.get("run_id"),
                    WorkflowRun.entity_id == entity_id,
                    WorkflowRun.workspace_id == workspace_id,
                    WorkflowRun.status.in_(("running", "paused", "waiting")),
                ))
                if active_run is not None:
                    continue
                if recovery == "verification_required":
                    if "verification_required" not in pending_stages:
                        # Preserve the fenced lease as recovery evidence. A
                        # later consumer may claim it only by explicitly
                        # declaring the verification-required stage.
                        row.current_stage, row.revision = "verification_required", row.revision + 1
                        row.state = state
                        await db.flush()
                        continue
                    # The consumer explicitly accepts recovery work. Under
                    # this row lock, fence the expired terminal owner and
                    # claim the same item for read-only reconciliation.
                    pending_stage = "verification_required"
            _set_state_value(state, lease_path, {
                "run_id": run_id,
                "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
                "due_path": due_path,
                "pending_stage": pending_stage,
            })
            row.state, row.current_stage, row.last_run_id, row.revision = state, "leased", run_id, row.revision + 1
            await db.flush()
            return row


async def complete_workflow_project_lease(
    db: AsyncSession, *, project_id: str, entity_id: str, workspace_id: str,
    run_id: str, stage: str, patch: dict | None = None, lease_path: str = "lease",
) -> WorkflowProject:
    if not stage.strip() or stage == "leased":
        raise ValueError("lease completion requires a non-leased stage")
    row = await db.scalar(select(WorkflowProject).where(WorkflowProject.id == project_id,
        WorkflowProject.entity_id == entity_id, WorkflowProject.workspace_id == workspace_id)
        .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise WorkflowProjectNotFound(project_id)
    state = deepcopy(row.state or {})
    lease = _state_value(state, lease_path) or {}
    if row.current_stage != "leased" or not isinstance(lease, dict) or lease.get("run_id") != run_id:
        raise PermissionError("workflow project lease is not owned by this run")
    if patch:
        state.update(patch)
    _set_state_value(state, lease_path, {})
    row.state, row.current_stage, row.last_run_id, row.revision = state, stage, run_id, row.revision + 1
    await db.flush()
    return row


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
    project_key: str | None = None,
) -> WorkflowProject:
    project = WorkflowProject(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        project_type=project_type,
        project_key=project_key,
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


async def claim_workflow_project(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    project_type: str,
    project_key: str,
    state: dict,
    created_by: str,
    schema_version: int = 1,
    current_stage: str = "draft",
    last_run_id: str | None = None,
) -> tuple[WorkflowProject, bool]:
    """Atomically claim one business execution key.

    Returns ``(project, True)`` for the winner and the existing project with
    ``False`` for duplicate dispatches. The unique business-key index is the
    concurrency boundary; the SAVEPOINT keeps a losing insert from poisoning
    the caller's transaction.
    """
    normalized_key = str(project_key or "").strip()
    if not normalized_key:
        raise ValueError("Workflow project claim requires project_key")

    project = WorkflowProject(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        project_type=project_type,
        project_key=normalized_key,
        schema_version=schema_version,
        current_stage=current_stage,
        state=dict(state),
        revision=0,
        last_run_id=last_run_id,
        created_by=created_by,
    )
    try:
        async with db.begin_nested():
            db.add(project)
            await db.flush()
    except IntegrityError:
        existing = (await db.execute(
            select(WorkflowProject).where(
                WorkflowProject.entity_id == entity_id,
                WorkflowProject.workspace_id == workspace_id,
                WorkflowProject.project_type == project_type,
                WorkflowProject.project_key == normalized_key,
            )
        )).scalar_one()
        return existing, False
    return project, True


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

    # A reference pinned after the caller read the state outlives its patch.
    current_state = (await db.execute(
        select(WorkflowProject.state).where(*filters).with_for_update()
    )).scalar_one_or_none()
    if current_state is not None:
        values["state"] = preserve_pinned_references(current_state, values["state"])

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
