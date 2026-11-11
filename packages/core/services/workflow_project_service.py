"""Concurrency-safe persistence for state shared by related workflow runs."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from copy import deepcopy
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.workflow import WORKFLOW_RUN_OPEN_STATUSES
from packages.core.models.base import generate_ulid
from packages.core.models.workflow import WorkflowProject, WorkflowRun
from packages.core.services.project_references import preserve_pinned_references
from packages.core.services.workspace_layout import PROJECT_ARTIFACT_SEGMENT


class WorkflowProjectConflict(RuntimeError):
    """Raised when a caller patches a stale project revision."""


class WorkflowProjectNotFound(LookupError):
    """Raised when a project is absent or outside the caller's entity."""


class WorkflowProjectKeyTaken(ValueError):
    """Raised when ``create`` names a business key another project already holds."""

    def __init__(self, existing: WorkflowProject):
        self.existing = existing
        super().__init__(
            f'A {existing.project_type.replace("_", " ")} project "{existing.project_key}" '
            f"already exists in this Workspace (stage: {existing.current_stage}). "
            "Start the run with a different value for its key, or make this step "
            "claim the existing project instead of creating one."
        )


_UNSET = object()

# ``WorkflowProject.project_key`` is a String(200).
PROJECT_KEY_MAX_LENGTH = 200


def bounded_project_key(project_key: str) -> str:
    """The key as stored: a longer one (a whole brief or email) keeps its start and a digest of the rest.

    The same text always gives the same key, so a repeat still finds its project.
    """
    if len(project_key) <= PROJECT_KEY_MAX_LENGTH:
        return project_key
    digest = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:16]
    return f"{project_key[:PROJECT_KEY_MAX_LENGTH - len(digest) - 1].rstrip()}~{digest}"

# A project in one of these stages is over: the scheduled work it owns stops
# (see ``retire_owned_work_if_ended``). Other stages, "completed" included, may
# keep follow-up work such as collecting results after the last delivery.
PROJECT_END_STAGES = frozenset({"ended", "cancelled"})


async def retire_owned_work_if_ended(db: AsyncSession, project: WorkflowProject) -> None:
    """Stop the jobs an ended or cancelled project owns, in the caller's transaction."""
    if project.current_stage not in PROJECT_END_STAGES:
        return
    from packages.core.services.scheduler_service import retire_project_owned_jobs

    await retire_project_owned_jobs(
        db, entity_id=project.entity_id, workspace_id=project.workspace_id, project_id=project.id,
        causation_id=project.last_run_id,
    )


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
    await retire_owned_work_if_ended(db, row)
    return row


async def release_failed_run_project_leases(
    db: AsyncSession, *, run_id: str, entity_id: str, workspace_id: str, lease_paths: set[str],
) -> list[str]:
    """Give back each project a failed run leased, restoring the stage it waited in.

    ``lease_due`` records that stage as ``pending_stage`` on the lease, so the
    release clears the lease and sets the project back to it. A later run, or a
    retry of this one, can then lease the project again. Projects that are no
    longer leased by this run (already released, re-leased, or deleted) are left
    alone, so a repeated call changes nothing.
    """
    candidate_ids = (await db.execute(select(WorkflowProject.id).where(
        WorkflowProject.entity_id == entity_id,
        WorkflowProject.workspace_id == workspace_id,
        WorkflowProject.current_stage == "leased",
    ))).scalars().all()
    released = []
    for project_id in candidate_ids:
        row = await db.scalar(select(WorkflowProject).where(
            WorkflowProject.id == project_id,
            WorkflowProject.entity_id == entity_id,
            WorkflowProject.workspace_id == workspace_id,
            WorkflowProject.current_stage == "leased",
        ).with_for_update().execution_options(populate_existing=True))
        if row is None:
            continue
        state = deepcopy(row.state or {})
        for lease_path in sorted(lease_paths):
            lease = _state_value(state, lease_path)
            if (not isinstance(lease, dict) or lease.get("run_id") != run_id
                    or not isinstance(lease.get("pending_stage"), str)
                    or lease["pending_stage"] in {"", "leased"}):
                continue
            _set_state_value(state, lease_path, {})
            row.state, row.current_stage, row.last_run_id, row.revision = (
                state, lease["pending_stage"], run_id, row.revision + 1,
            )
            released.append(project_id)
            break
    if released:
        await db.flush()
    return released


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
    if project_key is not None:
        project_key = bounded_project_key(project_key)
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
    # The SAVEPOINT keeps a duplicate business key from poisoning the
    # caller's transaction (a workflow run records its failure on it).
    try:
        async with db.begin_nested():
            db.add(project)
            await db.flush()
    except IntegrityError:
        if project_key is None:
            raise
        existing = (await db.execute(
            select(WorkflowProject).where(
                WorkflowProject.entity_id == entity_id,
                WorkflowProject.workspace_id == workspace_id,
                WorkflowProject.project_type == project_type,
                WorkflowProject.project_key == project_key,
            )
        )).scalar_one_or_none()
        if existing is None:
            raise
        raise WorkflowProjectKeyTaken(existing) from None
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
    normalized_key = bounded_project_key(str(project_key or "").strip())
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


async def open_run_project_marks(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    run_id: str | None = None,
) -> dict[str, str]:
    """Each project an open Flow run last wrote, by its id and its folder, mapped to that run.

    The run's later steps and approval own that work: text naming either mark is
    about that project. ``run_id`` narrows it to one run's own projects.
    """
    query = (
        select(WorkflowProject.id, WorkflowProject.state, WorkflowRun.id)
        .join(WorkflowRun, WorkflowRun.id == WorkflowProject.last_run_id)
        .where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
        )
    )
    if run_id:
        query = query.where(WorkflowRun.id == run_id)
    marks: dict[str, str] = {}
    for project_id, state, owner_run_id in (await db.execute(query)).all():
        folder = str((state or {}).get("project_root") or "").strip().strip("/") if isinstance(state, dict) else ""
        for mark in (project_id, folder):
            if mark:
                marks[mark] = owner_run_id
    return marks


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


class WorkflowProjectInUse(RuntimeError):
    """Raised when a project to delete is still owned by another open Flow run."""


# Child kinds one delete may also remove, so one call cannot sweep a Workspace.
DELETE_CHILD_KINDS_MAX = 5


async def delete_workflow_project(
    db: AsyncSession,
    *,
    project_id: str,
    entity_id: str,
    workspace_id: str,
    project_type: str,
    children: list[tuple[str, str]] = (),
    run_id: str | None = None,
    expected_child_ids: list[str] | None = None,
) -> list[str]:
    """Permanently delete a project, its listed child projects and the jobs they own.

    ``children`` names each child kind as (project_type, state key holding the
    parent's id). Every row is locked before its owner is checked, so a run
    that starts on one meanwhile waits for this delete or is refused by it. A
    project another open run last wrote (a post being published) is not
    deleted; the caller's own run does not count. ``expected_child_ids`` is the
    set the caller listed and cleaned up files for: if the children differ, the
    delete is refused, so nothing is removed that the caller did not clean up.
    Run history, files and Stats stay with their own owners. Returns the
    deleted project ids; the caller commits, and a refusal changes nothing.
    """
    from sqlalchemy import delete as sql_delete

    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowActionGrant
    from packages.core.services.scheduler_service import delete_scheduled_job

    project = (await db.execute(select(WorkflowProject).where(
        WorkflowProject.id == project_id,
        WorkflowProject.entity_id == entity_id,
        WorkflowProject.workspace_id == workspace_id,
    ).with_for_update())).scalar_one_or_none()
    if project is None:
        raise WorkflowProjectNotFound(project_id)
    if project.project_type != project_type:
        raise ValueError("Workflow project type does not match")
    rows = [project]
    for child_type, parent_key in list(children)[:DELETE_CHILD_KINDS_MAX]:
        rows += list((await db.execute(select(WorkflowProject).where(
            WorkflowProject.entity_id == entity_id,
            WorkflowProject.workspace_id == workspace_id,
            WorkflowProject.project_type == child_type,
            WorkflowProject.state[parent_key].as_string() == project.id,
        ).with_for_update())).scalars())
    ids = [row.id for row in rows]
    if expected_child_ids is not None and {row.id for row in rows[1:]} != set(expected_child_ids):
        raise WorkflowProjectInUse(
            "the projects under this one changed since they were listed; nothing was deleted, try again"
        )
    busy = (await db.execute(select(WorkflowProject.id).join(
        WorkflowRun, WorkflowRun.id == WorkflowProject.last_run_id,
    ).where(
        WorkflowProject.id.in_(ids),
        WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
        *([WorkflowRun.id != run_id] if run_id else []),
    ))).scalars().all()
    if busy:
        raise WorkflowProjectInUse(
            f"{len(busy)} of these projects are still being worked on by another run; try again when it finishes"
        )
    jobs = (await db.execute(select(ScheduledJob.id).where(
        ScheduledJob.entity_id == entity_id,
        ScheduledJob.workspace_id == workspace_id,
        ScheduledJob.owner_project_id.in_(ids),
    ))).scalars().all()
    for job_id in jobs:
        await delete_scheduled_job(db, job_id, entity_id, allow_agent_initiative_driver=True)
    await db.execute(sql_delete(WorkflowActionGrant).where(
        WorkflowActionGrant.entity_id == entity_id, WorkflowActionGrant.project_id.in_(ids),
    ))
    await db.execute(sql_delete(WorkflowProject).where(WorkflowProject.id.in_(ids)))
    await db.flush()
    return ids


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
    if current_stage is not None:
        await retire_owned_work_if_ended(db, project)
    return project
