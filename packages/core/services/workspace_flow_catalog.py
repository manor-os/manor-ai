"""Canonical identity and lookup for Blueprint-installed Workspace Flows.

The Strategist context, review briefing, proposal validator and final
dispatcher all need to answer the same question: which active binding is the
Flow named by ``(blueprint_slug, workflow_slug)``?  Keeping that knowledge in
one catalog prevents each caller from interpreting workspace settings and
binding config slightly differently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.workflow import WORKFLOW_RUN_OPEN_STATUSES
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowRun,
)
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_workflow_router import (
    WorkspaceChatEntrypoint,
    normalize_chat_entrypoint,
)


class WorkspaceFlowCatalogError(ValueError):
    """A requested Blueprint Flow does not resolve to one callable binding."""


@dataclass(frozen=True)
class WorkspaceFlow:
    """One active, user-facing Workspace Flow and its canonical identity."""

    blueprint_slug: str
    workflow_slug: str
    workspace: Workspace
    binding: WorkflowBinding
    workflow: WorkflowDefinition
    entrypoint: WorkspaceChatEntrypoint

    def descriptor(self) -> dict[str, Any]:
        """Prompt-safe descriptor consumed by Strategist context."""
        return {
            "blueprint_slug": self.blueprint_slug,
            "workflow_slug": self.workflow_slug,
            "binding_id": self.binding.id,
            "title": self.entrypoint.title,
            "description": self.entrypoint.description,
            "inputs": [dict(item) for item in self.entrypoint.run_inputs],
        }


def workspace_blueprint_slug(workspace: Workspace | None) -> str:
    """Return the installed Blueprint slug, or ``""`` when absent."""
    settings = (
        workspace.settings
        if workspace is not None and isinstance(workspace.settings, dict)
        else {}
    )
    blueprint = (
        settings.get("_blueprint")
        if isinstance(settings.get("_blueprint"), dict)
        else {}
    )
    return str(blueprint.get("blueprint_slug") or "").strip()


def workspace_flow_slug(binding: WorkflowBinding | None) -> str:
    """Return the Blueprint-local Flow slug stored on a binding."""
    config = (
        binding.config
        if binding is not None and isinstance(binding.config, dict)
        else {}
    )
    return str(config.get("workspace_blueprint_workflow_slug") or "").strip()


def _normalize_flow(
    workspace: Workspace,
    binding: WorkflowBinding,
    workflow: WorkflowDefinition,
) -> WorkspaceFlow | None:
    blueprint_slug = workspace_blueprint_slug(workspace)
    workflow_slug = workspace_flow_slug(binding)
    entrypoint = normalize_chat_entrypoint(binding, workflow)
    if not blueprint_slug or not workflow_slug or entrypoint is None:
        return None
    return WorkspaceFlow(
        blueprint_slug=blueprint_slug,
        workflow_slug=workflow_slug,
        workspace=workspace,
        binding=binding,
        workflow=workflow,
        entrypoint=entrypoint,
    )


def _active_flow_statement(workspace: Workspace):
    return (
        select(WorkflowBinding, WorkflowDefinition)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.entity_id == workspace.entity_id,
            WorkflowBinding.workspace_id == workspace.id,
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )


async def list_workspace_flows(
    db: AsyncSession,
    workspace: Workspace,
) -> list[WorkspaceFlow]:
    """List active callable Flows in stable UI order."""
    if not workspace_blueprint_slug(workspace):
        return []
    rows = (
        await db.execute(
            _active_flow_statement(workspace).order_by(
                WorkflowBinding.name.asc(),
                WorkflowBinding.id.asc(),
            )
        )
    ).all()
    return [
        flow
        for binding, workflow in rows
        if (flow := _normalize_flow(workspace, binding, workflow)) is not None
    ]


async def resolve_workspace_flow(
    db: AsyncSession,
    *,
    workspace: Workspace,
    blueprint_slug: str,
    workflow_slug: str,
    lock_binding: bool = False,
) -> WorkspaceFlow:
    """Resolve one Flow identity, optionally locking its binding for dispatch.

    The final proposal dispatcher uses ``lock_binding=True`` so two approved
    items for the same Flow serialize their last-moment duplicate check.
    """
    requested_blueprint = str(blueprint_slug or "").strip()
    requested_workflow = str(workflow_slug or "").strip()
    if not requested_blueprint or not requested_workflow:
        raise WorkspaceFlowCatalogError(
            "workflow_ref requires blueprint_slug and workflow_slug"
        )
    if workspace_blueprint_slug(workspace) != requested_blueprint:
        raise WorkspaceFlowCatalogError(
            "Referenced Blueprint is not installed in this Workspace"
        )

    matches = [
        flow
        for flow in await list_workspace_flows(db, workspace)
        if flow.workflow_slug == requested_workflow
    ]
    if len(matches) != 1:
        raise WorkspaceFlowCatalogError(
            "Referenced Workspace Flow is unavailable or ambiguous"
        )
    resolved = matches[0]
    if not lock_binding:
        return resolved

    row = (
        await db.execute(
            _active_flow_statement(workspace)
            .where(WorkflowBinding.id == resolved.binding.id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise WorkspaceFlowCatalogError(
            "Referenced Workspace Flow became unavailable before dispatch"
        )
    locked = _normalize_flow(workspace, row[0], row[1])
    if locked is None or locked.workflow_slug != requested_workflow:
        raise WorkspaceFlowCatalogError(
            "Referenced Workspace Flow changed before dispatch"
        )
    return locked


async def latest_open_flow_run(
    db: AsyncSession,
    *,
    workspace: Workspace,
    binding_id: str,
) -> WorkflowRun | None:
    """Return the newest live lineage for a binding, if one exists."""
    return (
        await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.entity_id == workspace.entity_id,
                WorkflowRun.workspace_id == workspace.id,
                WorkflowRun.binding_id == binding_id,
                WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
            )
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
