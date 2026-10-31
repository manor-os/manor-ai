"""Derived Workspace autonomy state shared by runtime entry points."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Protocol


class WorkspaceAutonomyState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    DISABLED = "disabled"
    DELETED = "deleted"
    INACTIVE = "inactive"


class WorkspaceAutonomyFields(Protocol):
    status: str
    heartbeat_enabled: bool
    deleted_at: datetime | None


def workspace_allows_configured_work(workspace: WorkspaceAutonomyFields | None) -> bool:
    """An available Workspace can run deployed work with its own lifecycle.

    This is only a lifecycle check; callers still enforce actor, deployment,
    run-origin, readiness, approval and credit boundaries.
    """
    return bool(
        workspace is not None
        and workspace.deleted_at is None
        and workspace.status in {"active", "paused"}
    )


def workspace_allows_manual_work(workspace: WorkspaceAutonomyFields | None) -> bool:
    """Pausing autonomy does not disable user-directed Workspace work."""
    return workspace_allows_configured_work(workspace)


def scheduled_job_is_independent(execution_type: str | None) -> bool:
    """Deployed Workflows and collectors own their enabled state.

    Goal-linked collector schedules are additionally reconciled by the Goal
    runtime; Strategist, Goal and initiative drivers remain autonomy-owned.
    """
    return execution_type in {"workflow", "workspace_stat_collection"}


def resolve_workspace_autonomy_state(
    *,
    status: str,
    heartbeat_enabled: bool,
    deleted_at: datetime | None,
) -> WorkspaceAutonomyState:
    if deleted_at is not None:
        return WorkspaceAutonomyState.DELETED
    if status == "paused":
        return WorkspaceAutonomyState.PAUSED
    if status != "active":
        return WorkspaceAutonomyState.INACTIVE
    if not heartbeat_enabled:
        return WorkspaceAutonomyState.DISABLED
    return WorkspaceAutonomyState.RUNNING


def workspace_autonomy_state(
    workspace: WorkspaceAutonomyFields,
) -> WorkspaceAutonomyState:
    return resolve_workspace_autonomy_state(
        status=workspace.status,
        heartbeat_enabled=bool(workspace.heartbeat_enabled),
        deleted_at=workspace.deleted_at,
    )


def workspace_autonomy_skip_reason(
    workspace: WorkspaceAutonomyFields,
) -> str | None:
    state = workspace_autonomy_state(workspace)
    if state is WorkspaceAutonomyState.RUNNING:
        return None
    if state is WorkspaceAutonomyState.PAUSED:
        return "workspace_paused"
    if state is WorkspaceAutonomyState.DISABLED:
        return "workspace_runtime_disabled"
    if state is WorkspaceAutonomyState.DELETED:
        return "workspace_not_found"
    return f"workspace_{workspace.status}"
