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
