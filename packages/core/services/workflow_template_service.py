"""Stable, server-owned Flow templates and idempotent installation.

Template ids are portable catalogue identity. Installed ``workflow_id`` values
remain the only runtime identity used by Workspace bindings, Tasks,
Automations, Proposals, and sub-workflows.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.workflow_dependencies import WorkflowDependencyFactory
from packages.core.models.base import generate_ulid
from packages.core.models.workflow import (
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_payload_references,
)
from packages.core.services.workflow_run_trace import visible_workflow_tags


@dataclass(frozen=True)
class FlowTemplateSpec:
    id: str
    key: str
    name: str
    description: str
    icon: str
    version: str
    trigger_type: str
    category: str
    tags: tuple[str, ...]
    requirements: tuple[str, ...]
    source_blueprint_id: str
    values: dict[str, Any]
    visible: bool = True




@lru_cache(maxsize=1)
def _template_specs() -> dict[str, FlowTemplateSpec]:
    specs: dict[str, FlowTemplateSpec] = {}
    return specs


def get_flow_template(template_id: str) -> FlowTemplateSpec | None:
    return _template_specs().get(template_id)


def _dependency_keys(spec: FlowTemplateSpec) -> list[str]:
    keys: list[str] = []
    for step in spec.values.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") not in {
            "subworkflow", "foreach_subworkflow",
        }:
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        key = str(config.get("workflow_id") or "").strip()
        if key:
            keys.append(key)
    return list(dict.fromkeys(keys))


def _template_payload(
    spec: FlowTemplateSpec,
    *,
    installed_workflow_id: str | None = None,
) -> dict[str, Any]:
    specs = _template_specs()
    dependency_ids = [
        candidate.id
        for key in _dependency_keys(spec)
        for candidate in specs.values()
        if candidate.key == key
    ]
    return {
        "id": spec.id,
        "key": spec.key,
        "name": spec.name,
        "description": spec.description,
        "icon": spec.icon,
        "version": spec.version,
        "trigger_type": spec.trigger_type,
        "category": spec.category,
        "tags": list(spec.tags),
        "requirements": list(spec.requirements),
        "source_blueprint_id": spec.source_blueprint_id,
        "verification_status": "graph_verified",
        "node_count": len(spec.values.get("steps") or []),
        "dependency_ids": dependency_ids,
        "installed": installed_workflow_id is not None,
        "installed_workflow_id": installed_workflow_id,
    }


async def list_flow_templates(db: AsyncSession, entity_id: str) -> list[dict[str, Any]]:
    specs = _template_specs()
    installed_rows = list((await db.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.template_id.in_(list(specs)),
            WorkflowTemplateInstallation.component_key == "main",
        )
    )).scalars().all())
    installed_by_template = {row.template_id: row.workflow_id for row in installed_rows}
    return [
        _template_payload(
            spec,
            installed_workflow_id=installed_by_template.get(spec.id),
        )
        for spec in specs.values()
        if spec.visible
    ]


async def _installed_workflow(
    db: AsyncSession,
    *,
    entity_id: str,
    template_id: str,
) -> tuple[WorkflowTemplateInstallation | None, WorkflowDefinition | None]:
    installation = (await db.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.template_id == template_id,
            WorkflowTemplateInstallation.component_key == "main",
        )
    )).scalar_one_or_none()
    if installation is None:
        return None, None
    workflow = (await db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.id == installation.workflow_id,
        )
    )).scalar_one_or_none()
    if workflow is not None:
        return installation, workflow
    await db.delete(installation)
    await db.flush()
    return None, None


async def install_flow_template(
    db: AsyncSession,
    *,
    template_id: str,
    entity_id: str,
    installed_by: str,
    workspace_id: str | None = None,
    _installing: set[str] | None = None,
) -> dict[str, Any]:
    """Install one template and its referenced subflows by stable id."""
    specs = _template_specs()
    spec = specs.get(template_id)
    if spec is None:
        raise LookupError("Flow template not found")

    installing = _installing if _installing is not None else set()
    if template_id in installing:
        raise ValueError(f"Circular Flow template dependency: {template_id}")
    installing.add(template_id)
    try:
        installation, workflow = await _installed_workflow(
            db,
            entity_id=entity_id,
            template_id=template_id,
        )
        already_installed = workflow is not None

        dependency_results: list[dict[str, Any]] = []
        dependency_workflow_ids: dict[str, str] = {}
        for dependency_key in _dependency_keys(spec):
            dependency = next(
                (item for item in specs.values() if item.key == dependency_key),
                None,
            )
            if dependency is None:
                raise ValueError(
                    f"Flow template {template_id} references missing dependency {dependency_key!r}"
                )
            result = await install_flow_template(
                db,
                template_id=dependency.id,
                entity_id=entity_id,
                installed_by=installed_by,
                workspace_id=workspace_id,
                _installing=installing,
            )
            dependency_results.append(result)
            dependency_workflow_ids[dependency.key] = result["workflow"]["id"]

        if workflow is None:
            values = deepcopy(spec.values)
            steps = list(values.pop("steps", []))
            values.pop("name", None)
            steps = WorkflowDependencyFactory.to_runtime(
                steps,
                workflow_id_by_key=dependency_workflow_ids,
            )
            await lock_reusable_resource_payload_references(
                db,
                entity_id=entity_id,
                payload=steps,
            )
            workflow = WorkflowDefinition(
                id=generate_ulid(),
                entity_id=entity_id,
                created_by=installed_by,
                workspace_id=None,
                visibility="entity",
                name=spec.name,
                icon=spec.icon,
                steps=steps,
                **values,
            )
            db.add(workflow)
            await db.flush()
            installation = WorkflowTemplateInstallation(
                id=generate_ulid(),
                entity_id=entity_id,
                template_id=spec.id,
                component_key="main",
                workflow_id=workflow.id,
                installed_version=spec.version,
                installed_by=installed_by,
                source_type="flow_template",
                installation_metadata={
                    "source_blueprint_id": spec.source_blueprint_id,
                    "source_key": spec.key,
                },
            )
            db.add(installation)
            await db.flush()

        binding = None
        if workspace_id:
            from packages.core.services import workflow_service

            bindings = await workflow_service.list_bindings(
                db,
                entity_id,
                workspace_id=workspace_id,
                workflow_id=workflow.id,
            )
            binding = next((item for item in bindings if item.trigger_type == "manual"), None)
            if binding is None:
                binding = await workflow_service.create_workflow_binding(
                    db,
                    entity_id=entity_id,
                    workflow_id=workflow.id,
                    workspace_id=workspace_id,
                    name=workflow.name,
                    trigger_type="manual",
                    config={
                        "source": "flow_template",
                        "template_id": spec.id,
                    },
                )

        return {
            "template": _template_payload(spec, installed_workflow_id=workflow.id),
            "workflow": {
                "id": workflow.id,
                "entity_id": workflow.entity_id,
                "name": workflow.name,
                "description": workflow.description,
                "icon": workflow.icon,
                "trigger_type": workflow.trigger_type,
                "trigger_config": workflow.trigger_config or {},
                "steps": workflow.steps or [],
                "variables": workflow.variables or {},
                "category": workflow.category,
                "tags": visible_workflow_tags(workflow.tags),
                "status": workflow.status,
                "is_active": workflow.is_active,
                "version": workflow.version,
                "created_by": workflow.created_by,
                "created_at": workflow.created_at,
                "updated_at": workflow.updated_at,
            },
            "binding_id": binding.id if binding is not None else None,
            "already_installed": already_installed,
            "dependencies": dependency_results,
        }
    finally:
        installing.remove(template_id)
