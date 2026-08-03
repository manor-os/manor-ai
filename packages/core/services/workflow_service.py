"""Workflow service — CRUD for definitions & runs, step execution engine."""
from __future__ import annotations

from copy import deepcopy
import logging
from datetime import datetime, timezone
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, with_expression

from packages.core.ai.workflow_import import import_workflow
from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.user import User
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowProject,
    WorkflowRun,
)
from packages.core.models.workspace import Workspace
from packages.core.services.workflow_run_trace import (
    DEFINITION_CHANGED_ERROR,
    append_execution_trace,
    build_definition_snapshot,
    summarize_trace_text,
    workflow_definition_changed,
    workflow_definition_fingerprint,
)

logger = logging.getLogger(__name__)


SUPPORTED_WORKFLOW_STEP_TYPES = set(CANONICAL_NODE_TYPES) | {"webhook", "note"}
RUN_FAMILY_MAX_RUNS = 200
RUN_RESERVED_TRIGGER_FIELDS = {
    "retry_of_run_id",
    "retry_from_step_id",
    "attempt_number",
    "_workflow_history_summary",
}


def _run_summary_options():
    return (
        load_only(
            WorkflowRun.id,
            WorkflowRun.workflow_id,
            WorkflowRun.entity_id,
            WorkflowRun.workspace_id,
            WorkflowRun.binding_id,
            WorkflowRun.trigger_source,
            WorkflowRun.retry_of_run_id,
            WorkflowRun.retry_from_step_id,
            WorkflowRun.lineage_root_run_id,
            WorkflowRun.lineage_is_legacy,
            WorkflowRun.attempt_number,
            WorkflowRun.status,
            WorkflowRun.current_step_id,
            WorkflowRun.started_by,
            WorkflowRun.started_at,
            WorkflowRun.completed_at,
            WorkflowRun.created_at,
            WorkflowRun.updated_at,
            raiseload=True,
        ),
        with_expression(
            WorkflowRun.summary_workflow_name,
            func.left(WorkflowRun.trigger_data["_workflow_name"].as_string(), 255),
        ),
        with_expression(
            WorkflowRun.summary_current_step_name,
            func.left(
                func.jsonb_extract_path_text(
                    WorkflowRun.trigger_data,
                    "_workflow_node_names",
                    WorkflowRun.current_step_id,
                ),
                255,
            ),
        ),
        with_expression(
            WorkflowRun.summary_history_state,
            WorkflowRun.trigger_data["_workflow_history_summary"],
        ),
        with_expression(
            WorkflowRun.summary_legacy_retry_of_run_id,
            func.left(
                WorkflowRun.trigger_data["retry_of_run_id"].as_string(),
                100,
            ),
        ),
        with_expression(
            WorkflowRun.summary_legacy_retry_from_step_id,
            func.left(
                WorkflowRun.trigger_data["retry_from_step_id"].as_string(),
                100,
            ),
        ),
    )

# ── Workflow Definitions ──

def entry_step_id(steps: list[dict] | None) -> str | None:
    """Return the graph's explicit trigger/webhook entry node.

    A workflow definition may be saved while it is incomplete, but execution
    must never guess an entry from array order or node indegree.  That implicit
    fallback let API-created graphs run while the canvas correctly reported
    ``No trigger``.  Imported workflows are normalised with an explicit trigger
    before persistence, so every runnable graph now follows the same contract.
    """
    for step in steps or []:
        if step.get("type") in ("trigger", "webhook") and step.get("id"):
            return str(step["id"])
    return None


def require_entry_step_id(steps: list[dict] | None) -> str:
    """Return the explicit entry or reject an incomplete workflow graph."""
    entry = entry_step_id(steps)
    if entry is None:
        raise ValueError("Workflow requires an explicit trigger or webhook entry node")
    return entry


def _required_step_config_fields(step_type: str, config: dict) -> tuple[str, ...]:
    operation = str(config.get("operation") or "").strip().lower()
    if step_type == "workflow_project":
        if operation == "create":
            return ("project_type", "schema_version", "state_schema")
        if operation == "get":
            return ("project_id", "project_type", "schema_version", "state_schema")
        if operation == "patch":
            return (
                "project_id",
                "project_type",
                "schema_version",
                "expected_revision",
                "state_schema",
            )
        return ("operation",)
    if step_type == "workflow_action_grant":
        if operation == "create":
            return (
                "approval_step_id",
                "project_id",
                "grant_type",
                "scope",
                "scope_schema",
            )
        if operation == "revoke":
            return ("grant_id",)
        return ("operation",)
    if step_type == "browser_effect":
        required = ("record",)
        if operation == "transition":
            return (*required, "target_status")
        if operation not in {"", "decide"}:
            return (*required, "operation")
        return required
    return ()


def _step_outgoing_targets(
    step: dict,
    *,
    step_id: str,
    errors: list[dict[str, str]],
) -> list[str]:
    outgoing: list[str] = []
    for key in ("next", "true_next", "false_next"):
        raw = step.get(key, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            errors.append({
                "code": "invalid_edge_list",
                "message": f"Node '{step_id}' field '{key}' must be a list",
            })
            continue
        outgoing.extend(str(target) for target in raw if str(target).strip())

    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    if str(step.get("type") or "").strip().lower() == "switch":
        cases = config.get("cases") if isinstance(config.get("cases"), list) else []
        for case in cases:
            if isinstance(case, dict):
                targets = case.get("next", [])
                if isinstance(targets, list):
                    outgoing.extend(str(target) for target in targets if str(target).strip())
        defaults = config.get("default_next", [])
        if isinstance(defaults, list):
            outgoing.extend(str(target) for target in defaults if str(target).strip())
    return list(dict.fromkeys(outgoing))


def _validate_stage_config(
    stage_id: str,
    config: dict,
    errors: list[dict[str, str]],
) -> list[str]:
    for field in ("entry_operation_id", "operations", "routes"):
        if field not in config or config.get(field) in (None, ""):
            errors.append({
                "code": "invalid_node_config",
                "message": f"Node '{stage_id}' requires config.{field}",
            })

    entry_operation_id = str(config.get("entry_operation_id") or "").strip()
    operations = config.get("operations")
    routes = config.get("routes")
    if not isinstance(operations, list):
        if "operations" in config:
            errors.append({
                "code": "invalid_node_config",
                "message": f"Node '{stage_id}' config.operations must be a list",
            })
        operations = []
    if not isinstance(routes, dict):
        if "routes" in config:
            errors.append({
                "code": "invalid_node_config",
                "message": f"Node '{stage_id}' config.routes must be an object",
            })
        routes = {}

    operation_ids: list[str] = []
    seen_operation_ids: set[str] = set()
    operation_edges: dict[str, list[str]] = {}
    for index, operation_step in enumerate(operations):
        if not isinstance(operation_step, dict):
            errors.append({
                "code": "invalid_stage_operation",
                "message": (
                    f"Stage '{stage_id}' operation at index {index} must be an object"
                ),
            })
            continue
        operation_id = str(operation_step.get("id") or "").strip()
        if not operation_id:
            errors.append({
                "code": "missing_stage_operation_id",
                "message": f"Stage '{stage_id}' operation at index {index} has no id",
            })
            continue
        operation_ids.append(operation_id)
        if operation_id in seen_operation_ids:
            errors.append({
                "code": "duplicate_stage_operation_id",
                "message": (
                    f"Stage '{stage_id}' operation id '{operation_id}' is duplicated"
                ),
            })
        seen_operation_ids.add(operation_id)

        operation_type = str(operation_step.get("type") or "").strip().lower()
        if operation_type in {"stage", "subworkflow", "foreach_subworkflow"}:
            errors.append({
                "code": "invalid_stage_operation_type",
                "message": (
                    f"Stage '{stage_id}' operation '{operation_id}' cannot use "
                    f"nested type '{operation_type}'"
                ),
            })
        elif operation_type not in CANONICAL_NODE_TYPES or operation_type == "unsupported":
            errors.append({
                "code": "invalid_stage_operation_type",
                "message": (
                    f"Stage '{stage_id}' operation '{operation_id}' has unsupported "
                    f"type '{operation_type or 'missing'}'"
                ),
            })

        operation_config = (
            operation_step.get("config")
            if isinstance(operation_step.get("config"), dict)
            else {}
        )
        for field in _required_step_config_fields(operation_type, operation_config):
            if field not in operation_config or operation_config.get(field) in (None, ""):
                errors.append({
                    "code": "invalid_node_config",
                    "message": (
                        f"Stage '{stage_id}' operation '{operation_id}' "
                        f"requires config.{field}"
                    ),
                })
        operation_edges[operation_id] = _step_outgoing_targets(
            operation_step,
            step_id=f"{stage_id}.{operation_id}",
            errors=errors,
        )

    valid_operation_ids = set(operation_ids)
    if entry_operation_id and entry_operation_id not in valid_operation_ids:
        errors.append({
            "code": "missing_stage_entry_operation",
            "message": (
                f"Stage '{stage_id}' entry operation '{entry_operation_id}' does not exist"
            ),
        })

    route_names: set[str] = set()
    route_targets: list[str] = []
    for raw_name, raw_target in routes.items():
        route_name = str(raw_name).strip()
        if not route_name:
            errors.append({
                "code": "invalid_stage_route",
                "message": f"Stage '{stage_id}' contains an empty route name",
            })
            continue
        route_names.add(route_name)
        if route_name in valid_operation_ids:
            errors.append({
                "code": "invalid_stage_route",
                "message": (
                    f"Stage '{stage_id}' route '{route_name}' conflicts with an operation id"
                ),
            })
        if raw_target is None:
            continue
        route_target = str(raw_target).strip()
        if not route_target:
            errors.append({
                "code": "invalid_stage_route",
                "message": f"Stage '{stage_id}' route '{route_name}' has an empty target",
            })
            continue
        route_targets.append(route_target)

    for operation_id, targets in operation_edges.items():
        for target in targets:
            if target not in valid_operation_ids and target not in route_names:
                errors.append({
                    "code": "undeclared_stage_route",
                    "message": (
                        f"Stage '{stage_id}' operation '{operation_id}' points to "
                        f"undeclared route '{target}'"
                    ),
                })
    return list(dict.fromkeys(route_targets))


def validate_workflow_steps(steps: list[dict] | None) -> dict:
    """Validate a workflow graph for authoring, deployment, and execution.

    Errors make the graph unsafe to run. Warnings describe incomplete but
    saveable authoring states. This intentionally validates graph structure;
    node-specific credentials and external resources are resolved at runtime.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    nodes = steps if isinstance(steps, list) else []
    if not nodes:
        errors.append({"code": "empty_graph", "message": "Workflow has no nodes"})
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "entry_step_id": None,
            "node_count": 0,
            "edge_count": 0,
        }

    ids: list[str] = []
    seen: set[str] = set()
    entries: list[str] = []
    edge_count = 0
    edges_by_id: dict[str, list[str]] = {}

    for index, step in enumerate(nodes):
        if not isinstance(step, dict):
            errors.append({
                "code": "invalid_node",
                "message": f"Node at index {index} must be an object",
            })
            continue
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            errors.append({
                "code": "missing_node_id",
                "message": f"Node at index {index} has no id",
            })
            continue
        ids.append(step_id)
        if step_id in seen:
            errors.append({
                "code": "duplicate_node_id",
                "message": f"Node id '{step_id}' is duplicated",
            })
        seen.add(step_id)

        step_type = str(step.get("type") or "").strip().lower()
        if step_type not in SUPPORTED_WORKFLOW_STEP_TYPES:
            errors.append({
                "code": "unsupported_node_type",
                "message": f"Node '{step_id}' has unsupported type '{step_type or 'missing'}'",
            })
        elif step_type == "unsupported":
            errors.append({
                "code": "unmapped_node",
                "message": f"Node '{step_id}' must be replaced with a runnable Manor node",
            })
        if step_type in {"trigger", "webhook"}:
            entries.append(step_id)

        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        for field in _required_step_config_fields(step_type, config):
            if field not in config or config.get(field) in (None, ""):
                errors.append({
                    "code": "invalid_node_config",
                    "message": f"Node '{step_id}' requires config.{field}",
                })

        outgoing = _step_outgoing_targets(step, step_id=step_id, errors=errors)
        if step_type == "stage":
            outgoing.extend(_validate_stage_config(step_id, config, errors))
            outgoing = list(dict.fromkeys(outgoing))
        edges_by_id[step_id] = list(dict.fromkeys([
            *edges_by_id.get(step_id, []),
            *outgoing,
        ]))
        edge_count += len(outgoing)

    if not entries:
        errors.append({
            "code": "missing_entry",
            "message": "Workflow requires exactly one trigger or webhook entry node",
        })
    elif len(entries) > 1:
        errors.append({
            "code": "multiple_entries",
            "message": "Workflow has multiple trigger/webhook entry nodes: " + ", ".join(entries),
        })

    valid_ids = set(ids)
    for source, targets in edges_by_id.items():
        for target in targets:
            if target not in valid_ids:
                errors.append({
                    "code": "missing_edge_target",
                    "message": f"Node '{source}' points to missing node '{target}'",
                })

    entry = entries[0] if len(entries) == 1 else None
    if entry:
        reachable: set[str] = set()
        pending = [entry]
        while pending:
            current = pending.pop()
            if current in reachable or current not in valid_ids:
                continue
            reachable.add(current)
            pending.extend(edges_by_id.get(current, []))
        for step_id in ids:
            if step_id not in reachable:
                warnings.append({
                    "code": "unreachable_node",
                    "message": f"Node '{step_id}' is not reachable from entry '{entry}'",
                })

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "entry_step_id": entry,
        "node_count": len(nodes),
        "edge_count": edge_count,
    }


async def list_workflows(db: AsyncSession, entity_id: str) -> list[WorkflowDefinition]:
    result = await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.entity_id == entity_id)
        .order_by(WorkflowDefinition.created_at.desc())
    )
    return list(result.scalars().all())


async def get_workflow(db: AsyncSession, workflow_id: str, entity_id: str) -> WorkflowDefinition | None:
    result = await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.id == workflow_id, WorkflowDefinition.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def create_workflow(
    db: AsyncSession,
    entity_id: str,
    name: str,
    steps: list,
    *,
    created_by: str | None = None,
    description: str | None = None,
    icon: str = "flow",
    trigger_type: str = "manual",
    trigger_config: dict | None = None,
    variables: dict | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    workspace_id: str | None = None,
    visibility: str | None = None,
) -> WorkflowDefinition:
    wf = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=created_by,
        workspace_id=workspace_id,
        visibility=visibility or Visibility.ENTITY,
        name=name,
        steps=steps,
        description=description,
        icon=icon,
        trigger_type=trigger_type,
        trigger_config=trigger_config or {},
        variables=variables or {},
        category=category,
        tags=tags or [],
    )
    db.add(wf)
    await db.flush()
    await db.refresh(wf)
    return wf


async def get_workflow_metadata(
    db: AsyncSession,
    workflow_id: str,
    entity_id: str,
) -> dict | None:
    """Return provenance and deployment metadata for one scoped workflow."""
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return None

    creator = None
    if wf.created_by:
        creator = (await db.execute(
            select(User).where(
                User.id == wf.created_by,
                User.deleted_at.is_(None),
            )
        )).scalar_one_or_none()

    bindings = await list_bindings(
        db,
        entity_id,
        workflow_id=workflow_id,
    )
    workspace_ids = sorted({
        binding.workspace_id for binding in bindings if binding.workspace_id
    })
    workspaces_by_id: dict[str, Workspace] = {}
    if workspace_ids:
        workspace_rows = (await db.execute(
            select(Workspace).where(
                Workspace.id.in_(workspace_ids),
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalars().all()
        workspaces_by_id = {workspace.id: workspace for workspace in workspace_rows}

    creator_name = None
    if creator:
        creator_name = (
            creator.display_name
            or " ".join(filter(None, [creator.first_name, creator.last_name])).strip()
            or creator.email
        )

    return {
        "workflow_id": wf.id,
        "created_by": wf.created_by,
        "creator": (
            {"id": creator.id, "name": creator_name}
            if creator else None
        ),
        "created_at": wf.created_at,
        "updated_at": wf.updated_at,
        "version": wf.version,
        "status": wf.status,
        "trigger_type": wf.trigger_type,
        "binding_count": len(bindings),
        "workspace_count": len({
            binding.workspace_id for binding in bindings if binding.workspace_id
        }),
        "standalone_binding_count": sum(
            1 for binding in bindings if not binding.workspace_id
        ),
        "workspace_usage": [
            {
                "binding_id": binding.id,
                "binding_name": binding.name,
                "workspace_id": binding.workspace_id,
                "workspace_name": (
                    workspaces_by_id[binding.workspace_id].name
                    if binding.workspace_id in workspaces_by_id else None
                ),
                "business_line": binding.business_line,
                "trigger_type": binding.trigger_type,
                "enabled": binding.enabled,
                "status": binding.status,
                "created_at": binding.created_at,
            }
            for binding in bindings
            if binding.workspace_id
        ],
    }


# Template-CONTENT fields: changing one of these is a real behavioral change
# to every run of the workflow, so it bumps the M11 revision (+ audit row).
# Cosmetic fields (description, icon, category, tags, …) do not.
_WORKFLOW_CONTENT_REVISION_FIELDS = ("steps", "name", "variables")


async def update_workflow(
    db: AsyncSession, workflow_id: str, entity_id: str, **kwargs
) -> WorkflowDefinition | None:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return None
    content_patch: dict = {}
    for key, value in kwargs.items():
        if value is not None and hasattr(wf, key):
            if (
                key in _WORKFLOW_CONTENT_REVISION_FIELDS
                and getattr(wf, key) != value
            ):
                content_patch[key] = value
            setattr(wf, key, value)
    if kwargs:
        wf.version = int(wf.version or 1) + 1
    if content_patch:
        # M11: real content changes bump the config revision + audit row.
        from packages.core.revisions import bump_revision

        await bump_revision(db, wf, patch=content_patch)
    await db.flush()
    await db.refresh(wf)
    return wf


async def delete_workflow(db: AsyncSession, workflow_id: str, entity_id: str) -> bool:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return False
    await db.delete(wf)
    await db.flush()
    return True


# ── Workflow Bindings ──

async def create_workflow_binding(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str,
    *,
    workspace_id: str | None = None,
    business_line: str | None = None,
    name: str | None = None,
    trigger_type: str = "manual",
    trigger_config: dict | None = None,
    variables: dict | None = None,
    config: dict | None = None,
) -> WorkflowBinding:
    """Deploy a workflow definition into a run context (entity / workspace).

    Mirrors AgentSubscription: ``workspace_id=None`` is an entity/automation
    binding; a set ``workspace_id`` binds it into that workspace.

    For inbound triggers (webhook / event / workspace_event). Time-based
    scheduling is NOT a binding — it's an automation; use
    :func:`schedule_workflow`, which creates a ScheduledJob.
    """
    cfg = dict(trigger_config or {})
    # Webhook bindings get an auto-generated token (their inbound URL secret)
    if trigger_type == "webhook" and not cfg.get("webhook_token"):
        cfg["webhook_token"] = generate_ulid()

    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity_id,
        workflow_id=workflow_id,
        workspace_id=workspace_id,
        business_line=business_line,
        name=name,
        trigger_type=trigger_type,
        trigger_config=cfg,
        variables=variables or {},
        config=config or {},
    )
    db.add(binding)
    await db.flush()
    await db.refresh(binding)
    return binding


async def get_binding(
    db: AsyncSession, binding_id: str, entity_id: str,
) -> WorkflowBinding | None:
    return (await db.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.id == binding_id,
            WorkflowBinding.entity_id == entity_id,
        )
    )).scalar_one_or_none()


async def validate_workspace_workflow_reference(
    db: AsyncSession,
    entity_id: str,
    reference_id: str,
    *,
    workflow_id: str,
    workspace_id: str | None,
) -> WorkflowBinding:
    """Resolve and validate an automation's attached-workflow reference."""
    attached = await get_binding(db, reference_id, entity_id)
    if not attached:
        raise ValueError("Attached workspace workflow not found")
    if attached.trigger_type != "manual":
        raise ValueError("Automation must reference an attached workspace workflow")
    if attached.workflow_id != workflow_id:
        raise ValueError("Automation workflow does not match its workspace binding")
    if not workspace_id or attached.workspace_id != workspace_id:
        raise ValueError("Automation workflow binding belongs to another workspace")
    return attached


async def execution_binding_for_automation(
    db: AsyncSession,
    binding: WorkflowBinding,
) -> WorkflowBinding:
    """Return an attached binding when an automation points at one.

    Legacy event bindings without ``workspace_workflow_binding_id`` still run
    directly for backwards compatibility.
    """
    reference_id = (binding.config or {}).get("workspace_workflow_binding_id")
    if not reference_id:
        return binding
    return await validate_workspace_workflow_reference(
        db,
        binding.entity_id,
        str(reference_id),
        workflow_id=binding.workflow_id,
        workspace_id=binding.workspace_id,
    )


async def update_binding(
    db: AsyncSession,
    binding_id: str,
    entity_id: str,
    **changes,
) -> WorkflowBinding | None:
    binding = await get_binding(db, binding_id, entity_id)
    if not binding:
        return None
    for key in (
        "workflow_id", "workspace_id", "business_line", "name", "trigger_type",
        "trigger_config", "variables", "config", "enabled", "status",
    ):
        if key not in changes:
            continue
        value = changes[key]
        if value is None and key not in {"workspace_id", "business_line", "name"}:
            continue
        setattr(binding, key, value)
    if binding.trigger_type == "webhook":
        cfg = dict(binding.trigger_config or {})
        cfg.setdefault("webhook_token", generate_ulid())
        binding.trigger_config = cfg
    await db.flush()
    await db.refresh(binding)
    return binding


async def delete_binding(db: AsyncSession, binding_id: str, entity_id: str) -> bool:
    binding = await get_binding(db, binding_id, entity_id)
    if not binding:
        return False
    await db.delete(binding)
    await db.flush()
    return True


async def binding_automation_references(
    db: AsyncSession,
    binding: WorkflowBinding,
) -> list[str]:
    """Return automations that still reference a workspace workflow binding.

    References are intentionally stored by binding id so a workflow definition
    can be attached to several workspaces without an automation accidentally
    executing in the wrong workspace context.  Loading the small workspace
    collections and checking their JSON payloads in Python keeps this portable
    across PostgreSQL and the SQLite test database.
    """
    references: list[str] = []
    workspace_bindings = await list_bindings(
        db,
        binding.entity_id,
        workspace_id=binding.workspace_id,
    )
    for candidate in workspace_bindings:
        if candidate.id == binding.id:
            continue
        if (candidate.config or {}).get("workspace_workflow_binding_id") == binding.id:
            references.append(candidate.id)

    from packages.core.models.scheduler import ScheduledJob

    jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == binding.entity_id,
            ScheduledJob.workspace_id == binding.workspace_id,
            ScheduledJob.execution_type == "workflow",
        )
    )).scalars().all())
    for job in jobs:
        if (job.execution_target or {}).get("binding_id") == binding.id:
            references.append(job.id)
    return references


async def schedule_workflow(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str,
    *,
    cron: str,
    name: str | None = None,
    workspace_id: str | None = None,
    timezone_str: str = "UTC",
    created_by: str | None = None,
):
    """Schedule a workflow to run on a cron — as an **automation**.

    Scheduling is an automation concern, not a workflow binding: this creates a
    ScheduledJob (``execution_type="workflow"``) that lives in the Automations
    system and references the workflow via ``execution_target.workflow_id``. The
    existing Celery-Beat ``scheduler.tick`` then fires it, creating a WorkflowRun
    via the runner's ``exec_type="workflow"`` dispatch. The workflow itself stays
    independent — this is just the timer that calls it.
    """
    from packages.core.services.scheduler_service import create_scheduled_job

    job = await create_scheduled_job(
        db, entity_id,
        job_id=f"wf-{generate_ulid()}",
        name=name or "Scheduled workflow",
        schedule_kind="cron",
        cron_expr=str(cron),
        timezone_str=timezone_str,
        execution_type="workflow",
        workspace_id=workspace_id,
        user_id=created_by,
    )
    job.execution_target = {
        "workflow_id": workflow_id,
        **({"workspace_id": workspace_id} if workspace_id else {}),
    }
    await db.flush()
    return job


# ── Workflow Import (ComfyUI / n8n / Dify) ──

async def import_workflow_definition(
    db: AsyncSession,
    entity_id: str,
    raw,
    *,
    created_by: str | None = None,
    name: str | None = None,
    workspace_id: str | None = None,
    business_line: str | None = None,
    create_binding: bool = False,
) -> tuple[WorkflowDefinition, WorkflowBinding | None, dict]:
    """Import an exported workflow (ComfyUI/n8n/Dify) into a WorkflowDefinition.

    Returns ``(definition, binding_or_None, import_report_dict)``. Raises
    :class:`packages.core.ai.workflow_import.UnknownWorkflowFormat` if the
    payload can't be identified.
    """
    result = import_workflow(raw, name=name)
    report = result.report
    defn = result.definition

    wf = await create_workflow(
        db,
        entity_id=entity_id,
        created_by=created_by,
        name=defn["name"],
        steps=defn["steps"],
        variables=defn["variables"],
        category=business_line,
        tags=[f"imported:{report.source_tool}"],
        description=(
            f"Imported from {report.source_tool} — "
            f"{report.mapped}/{report.node_count} nodes mapped "
            f"({report.coverage:.0%} coverage), {report.unmapped_count} unmapped."
        ),
    )

    binding = None
    if create_binding and workspace_id:
        binding = await create_workflow_binding(
            db,
            entity_id=entity_id,
            workflow_id=wf.id,
            workspace_id=workspace_id,
            business_line=business_line,
            name=defn["name"],
        )

    return wf, binding, report.to_dict()


# ── Workflow Runs ──

def _workflow_display_metadata(workflow: WorkflowDefinition) -> dict:
    node_names = {
        str(step.get("id")): str(step.get("name") or step.get("id"))
        for step in (workflow.steps or [])
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    }
    return {
        "_workflow_name": str(workflow.name or workflow.id),
        "_workflow_node_names": node_names,
    }


def _snapshot_display_metadata(snapshot: dict) -> dict:
    node_names = {
        str(node.get("id")): str(node.get("name") or node.get("id"))
        for node in (snapshot.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }
    return {
        "_workflow_name": str(
            snapshot.get("name") or snapshot.get("workflow_id") or ""
        ),
        "_workflow_node_names": node_names,
    }

def _attempt_trigger_data(
    workflow: WorkflowDefinition,
    trigger_data: dict | None,
) -> dict:
    data = {
        key: deepcopy(value)
        for key, value in (trigger_data or {}).items()
        if key not in RUN_RESERVED_TRIGGER_FIELDS
    }
    data["_workflow_definition_version"] = int(workflow.version or 1)
    data["_workflow_definition_fingerprint"] = workflow_definition_fingerprint(workflow)
    data.update(_workflow_display_metadata(workflow))
    return data


def _selected_step_targets(step: dict, result: dict | None) -> list[str]:
    if result is not None and "next_override" in result:
        value = result.get("next_override")
        if value in (None, ""):
            return []
        return [str(item) for item in (value if isinstance(value, list) else [value])]
    value = step.get("next") or []
    return [str(item) for item in (value if isinstance(value, list) else [value])]


def retry_inherited_step_ids(
    steps: list[dict],
    prior_results: dict[str, dict],
    retry_from_step_id: str,
) -> set[str]:
    """Return completed ancestors that can be reused by a linked attempt."""
    reverse_edges: dict[str, set[str]] = {}
    for step in steps:
        step_id = str(step.get("id") or "")
        if not step_id:
            continue
        result = prior_results.get(step_id)
        for target in _selected_step_targets(step, result):
            reverse_edges.setdefault(target, set()).add(step_id)

    ancestors: set[str] = set()
    queue = list(reverse_edges.get(retry_from_step_id, set()))
    while queue:
        step_id = queue.pop(0)
        if step_id in ancestors:
            continue
        ancestors.add(step_id)
        queue.extend(reverse_edges.get(step_id, set()))
    return {
        step_id
        for step_id in ancestors
        if (prior_results.get(step_id) or {}).get("status") == "completed"
    }


def _retry_variables(
    workflow: WorkflowDefinition,
    prior: WorkflowRun,
    inherited_step_ids: set[str],
    variables: dict | None,
    retry_from_step_id: str | None = None,
) -> dict:
    updated = deepcopy(prior.variables or {})
    stage_execution = (
        updated.get("__stage_execution")
        if isinstance(updated.get("__stage_execution"), dict)
        else {}
    )
    retry_stage_id = str(retry_from_step_id or "").strip()
    retained_stage_state = stage_execution.get(retry_stage_id)
    if retry_stage_id and isinstance(retained_stage_state, dict):
        updated["__stage_execution"] = {
            retry_stage_id: deepcopy(retained_stage_state),
        }
    else:
        updated.pop("__stage_execution", None)
    prior_results = prior.step_results or {}

    def remove_step_outputs(step: dict, result: dict | None = None) -> None:
        step_id = str(step.get("id") or "")
        if not step_id:
            return
        updated.pop(step_id, None)
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        output_var = str(config.get("output_var") or "").strip()
        if output_var:
            updated.pop(output_var, None)
        for item in config.get("outputs") or []:
            if isinstance(item, dict):
                key = str(item.get("key") or item.get("name") or "").strip()
                if key:
                    updated.pop(key, None)
        operation_result = result if isinstance(result, dict) else {}
        if (
            step.get("type") == "transform"
            and (
                operation_result.get("status") == "completed"
                or operation_result.get("continued") is True
            )
        ):
            for key in config.get("set") or {}:
                updated.pop(str(key), None)
        output = operation_result.get("output")
        if isinstance(output, dict):
            for key, value in output.items():
                if updated.get(key) == value:
                    updated.pop(key, None)

    for step in workflow.steps or []:
        step_id = str(step.get("id") or "")
        if not step_id or step_id in inherited_step_ids:
            continue
        remove_step_outputs(step, prior_results.get(step_id))
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step.get("type") != "stage":
            continue
        stage_state = (
            retained_stage_state
            if step_id == retry_stage_id and isinstance(retained_stage_state, dict)
            else stage_execution.get(step_id)
        )
        operation_results = (
            stage_state.get("operation_results")
            if isinstance(stage_state, dict)
            and isinstance(stage_state.get("operation_results"), dict)
            else {}
        )
        for operation in config.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("id") or "")
            operation_result = operation_results.get(operation_id)
            keep_completed_retry_operation = (
                step_id == retry_stage_id
                and isinstance(operation_result, dict)
                and (
                    operation_result.get("status") == "completed"
                    or operation_result.get("continued") is True
                )
            )
            if not keep_completed_retry_operation:
                remove_step_outputs(operation, operation_result)
    for step in workflow.steps or []:
        step_id = str(step.get("id") or "")
        if step_id not in inherited_step_ids:
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        output_var = str(config.get("output_var") or "").strip()
        result = prior_results.get(step_id) or {}
        if output_var and result.get("status") == "completed" and "output" in result:
            updated[output_var] = deepcopy(result["output"])
    updated.pop("__result", None)
    if variables:
        updated.update(deepcopy(variables))
    return updated


async def _refresh_retry_workflow_project(
    db: AsyncSession,
    prior: WorkflowRun,
    variables: dict,
) -> dict:
    project_value = variables.get("project")
    project = project_value if isinstance(project_value, dict) else {}
    project_id = str(project.get("project_id") or "").strip()
    if not project_id:
        return variables
    durable = (await db.execute(
        select(WorkflowProject).where(
            WorkflowProject.id == project_id,
            WorkflowProject.entity_id == prior.entity_id,
            WorkflowProject.workspace_id == prior.workspace_id,
        )
    )).scalar_one_or_none()
    if durable is None:
        return variables
    refreshed = dict(variables)
    refreshed["project"] = {
        "project_id": durable.id,
        "project_type": durable.project_type,
        "schema_version": durable.schema_version,
        "current_stage": durable.current_stage,
        "state": deepcopy(durable.state or {}),
        "revision": durable.revision,
        "last_run_id": durable.last_run_id,
    }
    return refreshed


async def _retry_business_state(
    db: AsyncSession,
    prior: WorkflowRun,
) -> tuple[str | None, dict]:
    project_value = (prior.variables or {}).get("project")
    project = project_value if isinstance(project_value, dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    project_id = str(project.get("project_id") or "").strip()
    if project_id:
        durable = (await db.execute(
            select(WorkflowProject).where(
                WorkflowProject.id == project_id,
                WorkflowProject.entity_id == prior.entity_id,
                WorkflowProject.workspace_id == prior.workspace_id,
            )
        )).scalar_one_or_none()
        if durable is not None and isinstance(durable.state, dict):
            state = durable.state
    outcome = str(state.get("business_outcome") or "").strip() or None
    retry_state = state.get("retry_state")
    return outcome, retry_state if isinstance(retry_state, dict) else {}


def _validate_retry_variable_patch(
    workflow: WorkflowDefinition,
    prior: WorkflowRun,
    variables: dict | None,
) -> dict | None:
    if not variables:
        return variables
    if not isinstance(variables, dict):
        raise ValueError("Retry variables must be an object")

    from packages.core.services.workspace_workflow_router import (
        WorkspaceChatEntrypoint,
        assemble_workspace_workflow_inputs,
        validate_workspace_workflow_inputs,
        workflow_run_inputs,
    )

    run_inputs = workflow_run_inputs(workflow)
    allowed_keys = set((workflow.variables or {}).keys()) | {
        str(item.get("key") or "") for item in run_inputs
    }
    unknown_keys = sorted(set(variables) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            "Retry variables are not declared by the workflow: "
            + ", ".join(unknown_keys)
        )
    if not run_inputs:
        return deepcopy(variables)

    entrypoint = WorkspaceChatEntrypoint(
        binding_id="retry-validation",
        workflow_id=workflow.id,
        workspace_id=prior.workspace_id or "",
        title=workflow.name,
        description=workflow.description or "",
        placeholder="",
        order=0,
        run_inputs=run_inputs,
        intent_enabled=False,
        intent_description="",
        intent_examples=(),
        intent_negative_examples=(),
        minimum_confidence=0,
        projection={},
        wait_bridge=False,
    )
    missing = object()

    def path_value(source: dict, path: str):
        current: object = source
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return missing
            current = current[part]
        return current

    prior_variables = prior.variables if isinstance(prior.variables, dict) else {}
    provided_inputs: dict = {}
    for item in run_inputs:
        key = str(item["key"])
        target = str(item.get("target") or key)
        value = path_value(variables, key)
        if value is missing and target != key:
            value = path_value(variables, target)
        if value is missing:
            value = path_value(prior_variables, key)
        if value is missing and target != key:
            value = path_value(prior_variables, target)
        if value is not missing:
            provided_inputs[key] = deepcopy(value)

    validated_inputs = validate_workspace_workflow_inputs(
        entrypoint,
        provided_inputs,
    )
    normalized = deepcopy(variables)
    normalized.update(deepcopy(validated_inputs))
    normalized.update(assemble_workspace_workflow_inputs(entrypoint, validated_inputs))
    return normalized

async def start_workflow(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str,
    *,
    variables: dict | None = None,
    trigger_data: dict | None = None,
    started_by: str | None = None,
    workspace_id: str | None = None,
    binding_id: str | None = None,
    trigger_source: str = "manual",
) -> WorkflowRun:
    """Start a new workflow run."""
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        raise ValueError("Workflow not found")
    if not wf.is_active or wf.status != "active":
        raise ValueError("Workflow is inactive")

    # Merge workflow-level default variables with runtime overrides
    merged_vars = dict(wf.variables or {})
    if variables:
        merged_vars.update(variables)

    steps = wf.steps or []
    first_step_id = require_entry_step_id(steps)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)

    run_id = generate_ulid()
    run = WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        binding_id=binding_id,
        trigger_source=trigger_source,
        lineage_root_run_id=run_id,
        lineage_is_legacy=False,
        attempt_number=1,
        status="running",
        current_step_id=first_step_id,
        variables=merged_vars,
        step_results={},
        trigger_data=attempt_trigger_data,
        definition_snapshot=build_definition_snapshot(
            wf,
            fingerprint=attempt_trigger_data["_workflow_definition_fingerprint"],
        ),
        execution_trace=[],
        started_by=started_by,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    return run


async def start_workflow_from_binding(
    db: AsyncSession,
    binding: WorkflowBinding,
    *,
    trigger_data: dict | None = None,
    variables: dict | None = None,
    trigger_source: str | None = None,
    started_by: str | None = None,
    execution_workspace_id: str | None = None,
) -> WorkflowRun:
    """Start a run from a binding, resolving its run context.

    The run carries the binding's ``workspace_id`` / ``binding_id`` /
    ``trigger_source`` so the engine resolves the right workspace connectors,
    RAG, approvers and budget (see docs/design/workflow-engine.md §4.1).
    """
    wf = await get_workflow(db, binding.workflow_id, binding.entity_id)
    if not wf:
        raise ValueError("Workflow not found for binding")
    if not binding.enabled or binding.status != "active":
        raise ValueError("Workflow binding is inactive")
    if not wf.is_active or wf.status != "active":
        raise ValueError("Workflow is inactive")
    if (
        binding.workspace_id
        and execution_workspace_id
        and binding.workspace_id != execution_workspace_id
    ):
        raise ValueError("Workflow binding belongs to another workspace")
    effective_workspace_id = binding.workspace_id or execution_workspace_id

    merged_vars = dict(wf.variables or {})
    merged_vars.update(binding.variables or {})
    if variables:
        merged_vars.update(variables)
    # Make the trigger payload (webhook body / event data) usable inside the
    # workflow — both flattened as {{field}} and grouped under {{trigger}}.
    if isinstance(trigger_data, dict) and trigger_data:
        merged_vars.update(trigger_data)
        merged_vars["trigger"] = trigger_data

    steps = wf.steps or []
    first_step_id = require_entry_step_id(steps)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)

    run_id = generate_ulid()
    run = WorkflowRun(
        id=run_id,
        workflow_id=wf.id,
        entity_id=binding.entity_id,
        workspace_id=effective_workspace_id,
        binding_id=binding.id,
        trigger_source=trigger_source or binding.trigger_type,
        lineage_root_run_id=run_id,
        lineage_is_legacy=False,
        attempt_number=1,
        status="running",
        current_step_id=first_step_id,
        variables=merged_vars,
        step_results={},
        trigger_data=attempt_trigger_data,
        definition_snapshot=build_definition_snapshot(
            wf,
            fingerprint=attempt_trigger_data["_workflow_definition_fingerprint"],
        ),
        execution_trace=[],
        started_by=started_by,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    return run


# ── Workflow Bindings (cont.) + Triggers ──

async def list_bindings(
    db: AsyncSession,
    entity_id: str,
    *,
    workspace_id: str | None = None,
    business_line: str | None = None,
    workflow_id: str | None = None,
) -> list[WorkflowBinding]:
    q = select(WorkflowBinding).where(WorkflowBinding.entity_id == entity_id)
    if workspace_id is not None:
        q = q.where(WorkflowBinding.workspace_id == workspace_id)
    if business_line is not None:
        q = q.where(WorkflowBinding.business_line == business_line)
    if workflow_id is not None:
        q = q.where(WorkflowBinding.workflow_id == workflow_id)
    q = q.order_by(WorkflowBinding.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def dispatch_trigger(
    db: AsyncSession,
    entity_id: str,
    *,
    trigger_type: str,
    event_name: str | None = None,
    workspace_id: str | None = None,
    trigger_data: dict | None = None,
    started_by: str | None = None,
) -> list[WorkflowRun]:
    """Fire a trigger: start a run for every enabled binding that matches.

    Matching:
      - binding.enabled is True
      - binding.trigger_type == trigger_type
      - if workspace_id given, binding.workspace_id == workspace_id
      - if event_name given, binding.trigger_config["event"] == event_name

    Returns the started runs (one per matched binding).
    """
    q = select(WorkflowBinding).join(
        WorkflowDefinition,
        WorkflowDefinition.id == WorkflowBinding.workflow_id,
    ).where(
        WorkflowBinding.entity_id == entity_id,
        WorkflowBinding.enabled.is_(True),
        WorkflowBinding.status == "active",
        WorkflowBinding.trigger_type == trigger_type,
        WorkflowDefinition.is_active.is_(True),
        WorkflowDefinition.status == "active",
    )
    if workspace_id is not None:
        q = q.where(WorkflowBinding.workspace_id == workspace_id)
    bindings = list((await db.execute(q)).scalars().all())

    runs: list[WorkflowRun] = []
    for binding in bindings:
        if event_name is not None and (binding.trigger_config or {}).get("event") != event_name:
            continue
        execution_binding = await execution_binding_for_automation(db, binding)
        execution_trigger_data = dict(trigger_data or {})
        if execution_binding.id != binding.id:
            execution_trigger_data.setdefault("automation_binding_id", binding.id)
        run = await start_workflow_from_binding(
            db, execution_binding,
            trigger_data=execution_trigger_data,
            trigger_source=trigger_type,
            started_by=started_by,
        )
        runs.append(run)
    return runs


async def dispatch_webhook(
    db: AsyncSession,
    token: str,
    *,
    payload: dict | None = None,
) -> list[WorkflowRun]:
    """Fire webhook-triggered bindings matching an inbound token.

    The token is the binding's shared secret (``trigger_config.webhook_token``),
    so this lookup is cross-entity and needs no auth context — the token *is*
    the authorization. Returns the started runs.
    """
    q = select(WorkflowBinding).join(
        WorkflowDefinition,
        WorkflowDefinition.id == WorkflowBinding.workflow_id,
    ).where(
        WorkflowBinding.enabled.is_(True),
        WorkflowBinding.status == "active",
        WorkflowBinding.trigger_type == "webhook",
        WorkflowBinding.trigger_config["webhook_token"].astext == token,
        WorkflowDefinition.is_active.is_(True),
        WorkflowDefinition.status == "active",
    )
    bindings = list((await db.execute(q)).scalars().all())

    runs: list[WorkflowRun] = []
    for binding in bindings:
        run = await start_workflow_from_binding(
            db, binding, trigger_data=payload or {}, trigger_source="webhook",
        )
        runs.append(run)
    return runs


async def get_run(db: AsyncSession, run_id: str, entity_id: str) -> WorkflowRun | None:
    result = await db.execute(
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def get_run_summary(
    db: AsyncSession,
    run_id: str,
    entity_id: str,
) -> WorkflowRun | None:
    result = await db.execute(
        select(WorkflowRun)
        .options(*_run_summary_options())
        .where(WorkflowRun.id == run_id, WorkflowRun.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def list_run_family(
    db: AsyncSession,
    run_id: str,
    entity_id: str,
    *,
    limit: int = RUN_FAMILY_MAX_RUNS,
    selected_run: WorkflowRun | None = None,
) -> list[WorkflowRun]:
    """Return a bounded retry family with a constant number of indexed queries."""
    limit = max(1, min(int(limit), RUN_FAMILY_MAX_RUNS))
    selected = selected_run
    if selected is None:
        selected = await get_run_summary(db, run_id, entity_id)
    if selected is None:
        return []

    scope = [
        WorkflowRun.entity_id == entity_id,
        WorkflowRun.workflow_id == selected.workflow_id,
    ]
    if selected.workspace_id is None:
        scope.append(WorkflowRun.workspace_id.is_(None))
    else:
        scope.append(WorkflowRun.workspace_id == selected.workspace_id)

    lineage_root_run_id = selected.lineage_root_run_id or selected.id
    family_members = [
        WorkflowRun.lineage_root_run_id == lineage_root_run_id,
        WorkflowRun.id == selected.id,
    ]
    if selected.lineage_status == "legacy_untrusted_incomplete":
        legacy_parent_id = selected.summary_effective_retry_of_run_id
        if legacy_parent_id and legacy_parent_id != selected.id:
            family_members.append(WorkflowRun.id == legacy_parent_id)

    result = await db.execute(
        select(WorkflowRun)
        .options(*_run_summary_options())
        .where(*scope, or_(*family_members))
        .order_by(
            case((WorkflowRun.id == selected.id, 0), else_=1),
            WorkflowRun.created_at.desc(),
            WorkflowRun.id.desc(),
        )
        .limit(limit)
    )
    runs = list(result.scalars().all())
    return sorted(runs, key=lambda run: (str(run.created_at or ""), run.id))


async def latest_workflow_run_family_attempt(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    lock_root: bool = False,
) -> WorkflowRun:
    """Return the latest canonical attempt, optionally serializing family writes."""
    lineage_root_run_id = run.lineage_root_run_id or run.id
    if lock_root:
        root_id = (await db.execute(
            select(WorkflowRun.id).where(
                WorkflowRun.id == lineage_root_run_id,
                WorkflowRun.entity_id == run.entity_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if root_id is None:
            raise ValueError("Workflow lineage root not found")

    latest = (await db.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.entity_id == run.entity_id,
            or_(
                WorkflowRun.id == lineage_root_run_id,
                WorkflowRun.lineage_root_run_id == lineage_root_run_id,
            ),
        )
        .order_by(
            WorkflowRun.attempt_number.desc(),
            WorkflowRun.created_at.desc(),
            WorkflowRun.id.desc(),
        )
        .limit(1)
    )).scalar_one_or_none()
    if latest is None:
        raise ValueError("Workflow run family not found")
    return latest


async def retry_workflow_run(
    db: AsyncSession,
    *,
    run_id: str,
    entity_id: str,
    started_by: str,
    from_step_id: str | None = None,
    variables: dict | None = None,
) -> WorkflowRun:
    """Create a linked attempt that resumes from a prior run checkpoint."""
    prior = await get_run(db, run_id, entity_id)
    if prior is None:
        raise ValueError("Workflow run not found")
    latest_family_attempt = await latest_workflow_run_family_attempt(
        db,
        prior,
        lock_root=True,
    )
    if latest_family_attempt.id != prior.id and latest_family_attempt.status != "cancelled":
        raise ValueError("A newer workflow attempt already exists")
    business_outcome, retry_state = await _retry_business_state(db, prior)
    retryable_business_outcome = (
        prior.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    )
    if prior.status != "failed" and not retryable_business_outcome:
        raise ValueError("Workflow run is not retryable")

    workflow = await get_workflow(db, prior.workflow_id, entity_id)
    if workflow is None:
        raise ValueError("Workflow not found")
    prior_snapshot = (
        prior.definition_snapshot
        if isinstance(prior.definition_snapshot, dict)
        else {}
    )
    stored_fingerprint = str(
        prior_snapshot.get("fingerprint")
        or (prior.trigger_data or {}).get("_workflow_definition_fingerprint")
        or ""
    )
    current_fingerprint = workflow_definition_fingerprint(workflow)
    if stored_fingerprint and stored_fingerprint != current_fingerprint:
        raise ValueError("Workflow definition changed since the failed attempt")

    normalized_variables = _validate_retry_variable_patch(workflow, prior, variables)
    retry_step_id = str(
        from_step_id
        or retry_state.get("retry_from_step_id")
        or prior.current_step_id
        or ""
    ).strip()
    step_ids = {
        str(step.get("id"))
        for step in workflow.steps or []
        if step.get("id")
    }
    if retry_step_id not in step_ids:
        raise ValueError("Retry step is not part of the workflow")

    inherited_ids = retry_inherited_step_ids(
        workflow.steps or [],
        prior.step_results or {},
        retry_step_id,
    )
    inherited_results = {
        step_id: deepcopy((prior.step_results or {})[step_id])
        for step_id in inherited_ids
    }
    prior_trigger = {
        key: deepcopy(value)
        for key, value in (prior.trigger_data or {}).items()
        if key not in {
            "parent_run_id",
            "parent_step_id",
            "parent_foreach_item_key",
            "parent_foreach_progress_key",
            "_workflow_history_summary",
        }
    }
    lineage_root_run_id = prior.lineage_root_run_id or prior.id
    attempt_number = max(
        prior.effective_attempt_number,
        latest_family_attempt.effective_attempt_number,
    ) + 1
    retry_trigger = _attempt_trigger_data(workflow, prior_trigger)
    if prior_snapshot:
        retry_trigger.update(_snapshot_display_metadata(prior_snapshot))
    retry_trigger["input_patch"] = deepcopy(variables or {})
    retry_variables = _retry_variables(
        workflow,
        prior,
        inherited_ids,
        normalized_variables,
        retry_step_id,
    )
    retry_variables = await _refresh_retry_workflow_project(
        db,
        prior,
        retry_variables,
    )

    retry = WorkflowRun(
        id=generate_ulid(),
        workflow_id=prior.workflow_id,
        entity_id=prior.entity_id,
        workspace_id=prior.workspace_id,
        binding_id=prior.binding_id,
        trigger_source=prior.trigger_source,
        retry_of_run_id=prior.id,
        retry_from_step_id=retry_step_id,
        lineage_root_run_id=lineage_root_run_id,
        lineage_is_legacy=prior.lineage_status != "canonical",
        attempt_number=attempt_number,
        status="running",
        current_step_id=retry_step_id,
        variables=retry_variables,
        step_results=inherited_results,
        trigger_data=retry_trigger,
        definition_snapshot=(
            deepcopy(prior_snapshot)
            if prior_snapshot
            else build_definition_snapshot(
                workflow,
                fingerprint=current_fingerprint,
            )
        ),
        execution_trace=[],
        started_by=started_by,
        started_at=datetime.now(timezone.utc),
    )
    db.add(retry)
    await db.flush()
    await db.refresh(retry)
    return retry


async def list_runs(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str | None = None,
    workspace_id: str | None = None,
    binding_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    readable_workspace_ids: set[str] | None = None,
    summary: bool = False,
) -> list[WorkflowRun]:
    q = select(WorkflowRun).where(WorkflowRun.entity_id == entity_id)
    if summary:
        q = q.options(*_run_summary_options())
    if readable_workspace_ids is not None:
        q = q.where(or_(
            WorkflowRun.workspace_id.is_(None),
            WorkflowRun.workspace_id.in_(readable_workspace_ids),
        ))
    if workflow_id:
        q = q.where(WorkflowRun.workflow_id == workflow_id)
    if workspace_id:
        q = q.where(WorkflowRun.workspace_id == workspace_id)
    if binding_id:
        q = q.where(WorkflowRun.binding_id == binding_id)
    if status:
        q = q.where(WorkflowRun.status == status)
    q = q.order_by(WorkflowRun.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


# ── Step Execution Engine ──

async def execute_workflow_step(db: AsyncSession, run_id: str, entity_id: str) -> dict:
    """Execute one ready node through the canonical workflow runner.

    The manual ``/step`` API used to duplicate a small subset of runner logic,
    which made newer nodes (code, HTTP, media, loop, subworkflow, etc.) appear to
    succeed without executing. Reusing ``WorkflowRunner`` keeps full-run,
    single-node, and step-by-step behavior identical.
    """
    run = await get_run(db, run_id, entity_id)
    if not run or run.status not in ("running", "pending"):
        return {"error": "Run not active"}

    workflow = await get_workflow(db, run.workflow_id, entity_id)
    if not workflow:
        return {"error": "Workflow not found"}
    if workflow_definition_changed(workflow, run):
        run.status = "failed"
        run.error = DEFINITION_CHANGED_ERROR
        run.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return {"status": "failed", "error": DEFINITION_CHANGED_ERROR}

    from packages.core.ai.workflow_runner import WorkflowRunner, _continues_on_error

    runner = WorkflowRunner()
    runnable = runner._find_runnable_steps(workflow, run)
    if not runnable:
        if runner._all_steps_done(workflow, run):
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return {"status": "completed"}
        return {"error": "No runnable step"}

    step = runnable[0]
    append_execution_trace(run, node=step, status="running")
    result = await runner._execute_step_safe(step, run, db)
    if result.get("status") == "failed" and _continues_on_error(step):
        result["continued"] = True
    runner._record_step_result(step, result, run)
    append_execution_trace(
        run,
        node=step,
        status="skipped" if result.get("skipped") else result.get("status"),
        result=result,
    )

    if result.get("status") == "paused":
        run.status = "paused"
    elif result.get("status") == "failed" and not result.get("continued"):
        run.status = "failed"
        run.error = summarize_trace_text(
            result.get("error"),
            fallback=f"Step {step['id']} failed",
        )
        run.completed_at = datetime.now(timezone.utc)
    else:
        next_steps = runner._find_runnable_steps(workflow, run)
        if next_steps:
            run.current_step_id = next_steps[0]["id"]
        elif runner._all_steps_done(workflow, run):
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(run)
    result["next_step_id"] = run.current_step_id if run.status == "running" else None
    return result
