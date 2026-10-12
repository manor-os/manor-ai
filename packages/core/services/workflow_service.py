"""Workflow service — CRUD for definitions & runs, step execution engine."""
from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
import logging
from datetime import datetime, timezone
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only, with_expression

from packages.core.ai.workflow_import import import_workflow
from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.contracts.json_schema import SchemaContractValidatorFactory
from packages.core.models.base import generate_ulid
from packages.core.models.permission import Visibility
from packages.core.models.user import User
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowProject,
    WorkflowRun,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_autonomy import workspace_allows_manual_work
from packages.core.workspaces import is_sandbox_workspace
from packages.core.services.workflow_run_trace import (
    WORKFLOW_IMPORT_PROOF_TAG_PREFIX,
    WORKFLOW_IMPORT_SOURCE_TAG_PREFIX,
    DEFINITION_CHANGED_ERROR,
    append_execution_trace,
    build_definition_snapshot,
    build_execution_snapshot,
    execution_snapshot_for_run,
    summarize_trace_text,
    trusted_workflow_import_source,
    workflow_import_provenance_tags,
    workflow_definition_changed,
    workflow_execution_view,
    workflow_definition_fingerprint,
)
from packages.core.services.reusable_resource_locks import (
    RESOURCE_WORKFLOW,
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_payload_reference_delta,
    lock_reusable_resource_payload_references,
    lock_reusable_resource_reference,
    lock_reusable_resource_references,
)
from packages.core.constants.workflow import (
    WORKFLOW_RUN_TERMINAL_STATUSES,
    WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD,
    WorkflowRunStatus,
)
from packages.core.services.workflow_retry import (
    RETRY_CONTEXT_KEY,
    build_retry_context,
    merge_retry_evidence,
    retry_target,
)

logger = logging.getLogger(__name__)


SUPPORTED_WORKFLOW_STEP_TYPES = set(CANONICAL_NODE_TYPES) | {"webhook", "note"}
RUN_FAMILY_MAX_RUNS = 200
MANUAL_WORKFLOW_TRIGGER_SOURCES = {
    "manual", "workspace_chat", "global_chat", "agent_test",
    "public_webchat", "site_form",
}
DIRECT_RUN_INPUT_CHECKED_SOURCES = {"manual", "schedule"}
RUN_RESERVED_TRIGGER_FIELDS = {
    RETRY_CONTEXT_KEY,
    "retry_of_run_id",
    "retry_from_step_id",
    "attempt_number",
    "_workflow_history_summary",
    "_workspace_user_request",
    WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD,
}


def workflow_run_is_user_requested(run: WorkflowRun) -> bool:
    """Read server-owned manual admission from this exact durable run."""
    request = (run.trigger_data or {}).get("_workspace_user_request")
    return bool(
        isinstance(request, dict)
        and run.started_by
        and request.get("user_id") == run.started_by
    )


async def _assert_workspace_workflow_execution_ready(
    db: AsyncSession,
    *,
    workspace_id: str | None,
    entity_id: str,
    trigger_source: str = "manual",
    user_requested: bool = False,
) -> None:
    if not workspace_id:
        return
    workspace = (await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if workspace is None:
        raise ValueError("Workspace not found")
    manual_trigger = user_requested or trigger_source in MANUAL_WORKFLOW_TRIGGER_SOURCES
    if not workspace_allows_manual_work(workspace) or (
        not manual_trigger and workspace.status != "active"
    ):
        raise ValueError("Workflow execution requires an active Workspace")
    if is_sandbox_workspace(workspace):
        raise ValueError("Workspace simulation cannot start an ordinary Workflow run")
    from packages.core.services.workspace_readiness import (
        evaluate_workspace_blocking_setup,
    )

    setup_status = await evaluate_workspace_blocking_setup(db, workspace)
    if setup_status is not None and setup_status.blocks_work:
        raise ValueError(
            "Workspace setup is incomplete; normal Workflow runs are suspended"
        )


async def _lock_workspace_workflow_configuration_scope(
    db: AsyncSession,
    *,
    workspace_id: str | None,
    entity_id: str,
) -> None:
    """Require one live owning Workspace before persisting a deployment."""

    if not workspace_id:
        return
    workspace = (await db.execute(
        select(Workspace.id)
        .where(
            Workspace.id == workspace_id,
            Workspace.entity_id == entity_id,
            Workspace.deleted_at.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if workspace is None:
        raise ValueError("Workspace not found")


class WorkflowInUseError(ValueError):
    """A definition still has deployment or automation references."""

    def __init__(
        self,
        *,
        binding_ids: list[str],
        scheduled_job_ids: list[str],
    ) -> None:
        super().__init__(
            "Remove workflow deployments and scheduled automations before deleting it"
        )
        self.binding_ids = binding_ids
        self.scheduled_job_ids = scheduled_job_ids


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


def workflow_service_keys(steps: list[dict] | None) -> set[str]:
    """Collect Workspace service dependencies declared by a workflow graph.

    Only executable node configs participate. This avoids treating arbitrary
    nested input/schema dictionaries as nodes, while still covering durable
    stage operation lists that the runner executes from the same graph.
    """
    keys: set[str] = set()

    def visit(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            config = node.get("config") if isinstance(node.get("config"), dict) else {}
            if not str(config.get("agent_id") or "").strip():
                key = str(config.get("service_key") or "").strip()
                if key:
                    keys.add(key)
            visit(config.get("operations"))

    visit(steps)
    return keys


async def _validate_workspace_service_dependencies(
    db: AsyncSession,
    *,
    workflow: WorkflowDefinition,
    workspace_id: str | None,
) -> None:
    """Fail before Run creation when a Workspace graph lacks active services."""
    if not workspace_id:
        return
    required = workflow_service_keys(workflow.steps or [])
    if not required:
        return

    from packages.core.models.workspace import AgentSubscription

    rows = (await db.execute(
        select(AgentSubscription.service_key).where(
            AgentSubscription.entity_id == workflow.entity_id,
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
            AgentSubscription.service_key.in_(required),
        )
    )).scalars().all()
    missing = sorted(required - {str(value).strip() for value in rows if value})
    if missing:
        raise ValueError(
            "Workflow preflight missing active Workspace services: "
            + ", ".join(missing)
        )


def _required_step_config_fields(step_type: str, config: dict) -> tuple[str, ...]:
    operation = str(config.get("operation") or "").strip().lower()
    if step_type == "workflow_project":
        if operation == "create":
            return ("project_type", "schema_version", "state_schema")
        if operation == "claim":
            return (
                "project_type",
                "project_key",
                "schema_version",
                "state_schema",
            )
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
    if step_type == "publication_receipt":
        return ("receipt",)
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
    error_targets = config.get("error_next", [])
    if isinstance(error_targets, list):
        outgoing.extend(
            str(target) for target in error_targets if str(target).strip()
        )
    elif "error_next" in config:
        errors.append({
            "code": "invalid_edge_list",
            "message": f"Node '{step_id}' field 'config.error_next' must be a list",
        })
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


def _validate_tool_operation_denylist(
    node_id: str, node_type: str, config: dict, errors: list[dict[str, str]],
) -> None:
    """A denylist must name real tool operations; a misspelt one would deny nothing."""
    from difflib import get_close_matches

    from packages.core.contracts.step_execution import (
        TOOL_OPERATION_DENYLIST_KEY, parse_tool_operation_denylist,
    )

    def fail(message: str) -> None:
        errors.append({"code": "invalid_tool_operation_denylist", "message": f"Node '{node_id}' {message}"})

    for key in config:
        if key != TOOL_OPERATION_DENYLIST_KEY and get_close_matches(
            str(key), [TOOL_OPERATION_DENYLIST_KEY], n=1, cutoff=0.85,
        ):
            fail(f"has config.{key}; did you mean config.{TOOL_OPERATION_DENYLIST_KEY}?")
    if TOOL_OPERATION_DENYLIST_KEY not in config:
        return
    if node_type != "agent":
        fail(f"config.{TOOL_OPERATION_DENYLIST_KEY} only applies to agent nodes")
        return
    try:
        denylist = parse_tool_operation_denylist(config[TOOL_OPERATION_DENYLIST_KEY])
    except ValueError as exc:
        fail(f"config.{TOOL_OPERATION_DENYLIST_KEY} {exc}")
        return
    from packages.core.ai.runtime.tool_registry import runtime_tool_schema

    for tool_name, operations in denylist.items():
        schema = runtime_tool_schema(tool_name) or {}
        parameters = (schema.get("function") or schema).get("parameters") or {}
        operation = (parameters.get("properties") or {}).get("operation") or {}
        known = {str(value).lower() for value in operation.get("enum") or ()}
        if not schema:
            fail(f"config.{TOOL_OPERATION_DENYLIST_KEY} names unknown tool '{tool_name}'")
        elif not known:
            fail(f"config.{TOOL_OPERATION_DENYLIST_KEY}: tool '{tool_name}' has no enumerated operation argument")
        elif unknown := sorted(operations - known):
            fail(
                f"config.{TOOL_OPERATION_DENYLIST_KEY}: '{tool_name}' has no operation "
                + ", ".join(f"'{value}'" for value in unknown)
            )


def _validate_workflow_schema_contracts(
    nodes: object,
    errors: list[dict[str, str]],
    *,
    parent_id: str = "",
) -> None:
    """Validate top-level and nested executable node contracts before a run."""

    if not isinstance(nodes, list):
        return
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or index)
        qualified_id = f"{parent_id}.{node_id}" if parent_id else node_id
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        try:
            from packages.core.services.workflow_timeout_config import (
                resolve_workflow_node_timeout,
            )

            resolve_workflow_node_timeout(config)
        except ValueError as exc:
            errors.append({
                "code": "invalid_node_timeout",
                "message": f"Node '{qualified_id}' {exc}",
            })
        _validate_tool_operation_denylist(
            qualified_id, str(node.get("type") or "").strip().lower(), config, errors,
        )
        for side in ("input", "output"):
            key = f"{side}_schema"
            schema = config.get(key)
            if schema is None:
                continue
            try:
                SchemaContractValidatorFactory.build(schema)
            except Exception as exc:
                errors.append({
                    "code": f"invalid_{side}_schema",
                    "message": (
                        f"Node '{qualified_id}' has invalid config.{key}: {exc}"
                    ),
                })
        run_inputs = config.get("run_inputs")
        if isinstance(run_inputs, list):
            for input_index, run_input in enumerate(run_inputs):
                if not isinstance(run_input, dict) or "schema" not in run_input:
                    continue
                schema = run_input.get("schema")
                if schema is None:
                    continue
                input_key = str(
                    run_input.get("key") or run_input.get("name") or input_index
                )
                try:
                    SchemaContractValidatorFactory.build(schema)
                except Exception as exc:
                    errors.append({
                        "code": "invalid_input_schema",
                        "message": (
                            f"Node '{qualified_id}' run input '{input_key}' has "
                            f"an invalid schema: {exc}"
                        ),
                    })
        for nested_key in ("operations", "steps"):
            _validate_workflow_schema_contracts(
                config.get(nested_key),
                errors,
                parent_id=qualified_id,
            )


def _public_workflow_tags(tags: object) -> list[str]:
    values = tags if isinstance(tags, list) else []
    return [
        normalized
        for tag in values
        if (normalized := str(tag).strip())
        and not normalized.lower().startswith(
            (
                WORKFLOW_IMPORT_SOURCE_TAG_PREFIX,
                WORKFLOW_IMPORT_PROOF_TAG_PREFIX,
            )
        )
    ]


def _imported_workflow_source(workflow: object) -> str | None:
    return trusted_workflow_import_source(workflow)


def _is_imported_unmapped_node(step: dict, source: str) -> bool:
    meta = step.get("meta") if isinstance(step.get("meta"), dict) else {}
    return (
        meta.get("unmapped") is True
        and str(meta.get("source_tool") or "").strip().lower() == source
    )


_DERIVE_IMPORT_SOURCE = object()


def _require_runtime_workflow(
    workflow: object,
    *,
    steps: list[dict] | None = None,
    imported_source: str | None | object = _DERIVE_IMPORT_SOURCE,
) -> None:
    """Require a safe graph while preserving imported-node skip semantics."""

    runtime_steps = steps if steps is not None else getattr(workflow, "steps", None)
    trusted_source = (
        _imported_workflow_source(workflow)
        if imported_source is _DERIVE_IMPORT_SOURCE
        else imported_source
    )
    validation = validate_workflow_steps(
        runtime_steps,
        imported_source=(
            str(trusted_source).strip().lower() if trusted_source else None
        ),
    )
    if not validation["valid"]:
        messages = "; ".join(error["message"] for error in validation["errors"])
        raise ValueError(f"Workflow is invalid: {messages}")


def validate_workflow_steps(
    steps: list[dict] | None,
    *,
    imported_source: str | None = None,
) -> dict:
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

    _validate_workflow_schema_contracts(nodes, errors)

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
        elif step_type == "unsupported" and not (
            imported_source
            and _is_imported_unmapped_node(step, imported_source)
        ):
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


def validate_workflow_definition(workflow: object) -> dict:
    """Validate a persisted definition with its server-owned provenance."""

    return validate_workflow_steps(
        getattr(workflow, "steps", None),
        imported_source=_imported_workflow_source(workflow),
    )


async def list_workflows(db: AsyncSession, entity_id: str) -> list[WorkflowDefinition]:
    result = await db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.entity_id == entity_id)
        .order_by(WorkflowDefinition.created_at.desc())
    )
    return list(result.scalars().all())


async def query_workflows(
    db: AsyncSession,
    entity_id: str,
    *,
    query: str | None = None,
    statuses: Sequence[str] | None = None,
    categories: Sequence[str] | None = None,
    workspace_ids: Sequence[str] | None = None,
    created_by_ids: Sequence[str] | None = None,
    active: bool | None = None,
    created_after: str | datetime | None = None,
    created_before: str | datetime | None = None,
    updated_after: str | datetime | None = None,
    updated_before: str | datetime | None = None,
    version_min: int | None = None,
    version_max: int | None = None,
    limit: int | None = 50,
    offset: int = 0,
) -> tuple[list[WorkflowDefinition], int]:
    """Query definitions with OR-within-field and AND-across-field semantics."""
    conditions = [WorkflowDefinition.entity_id == entity_id]
    search = str(query or "").strip()
    if search:
        pattern = f"%{search}%"
        conditions.append(or_(
            WorkflowDefinition.name.ilike(pattern),
            WorkflowDefinition.description.ilike(pattern),
            WorkflowDefinition.category.ilike(pattern),
        ))
    for column, values, lowercase in (
        (WorkflowDefinition.status, statuses, True),
        (WorkflowDefinition.category, categories, True),
        (WorkflowDefinition.workspace_id, workspace_ids, False),
        (WorkflowDefinition.created_by, created_by_ids, False),
    ):
        normalized = _workflow_query_values(values, lowercase=lowercase)
        if normalized:
            expression = func.lower(column) if lowercase else column
            conditions.append(expression.in_(normalized))
    if active is not None:
        conditions.append(WorkflowDefinition.is_active.is_(active))
    _append_workflow_numeric_range(
        conditions,
        WorkflowDefinition.version,
        version_min,
        version_max,
        "version",
    )
    for column, after, before, field_name in (
        (WorkflowDefinition.created_at, created_after, created_before, "created"),
        (WorkflowDefinition.updated_at, updated_after, updated_before, "updated"),
    ):
        _append_workflow_datetime_range(
            conditions,
            column,
            after,
            before,
            field_name,
        )
    page = (
        select(WorkflowDefinition)
        .where(*conditions)
        .order_by(WorkflowDefinition.created_at.desc(), WorkflowDefinition.id.desc())
    )
    if limit is not None:
        page = page.limit(limit).offset(max(0, offset))
    elif offset:
        page = page.offset(max(0, offset))
    count_query = select(func.count()).select_from(WorkflowDefinition).where(*conditions)
    rows = list((await db.execute(page)).scalars().all())
    total = int((await db.execute(count_query)).scalar_one())
    return rows, total


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
    import_source: str | None = None,
    workspace_id: str | None = None,
    visibility: str | None = None,
) -> WorkflowDefinition:
    await lock_reusable_resource_payload_references(
        db,
        entity_id=entity_id,
        payload=steps,
    )
    workflow_tags = _public_workflow_tags(tags)
    normalized_import_source = str(import_source or "").strip().lower()
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
        tags=workflow_tags,
    )
    if normalized_import_source:
        wf.tags = [
            *workflow_tags,
            *workflow_import_provenance_tags(wf, normalized_import_source),
        ]
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
    template_sources = list((await db.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.workflow_id == workflow_id,
        )
    )).scalars().all())
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
        "template_sources": [
            {
                "template_id": source.template_id,
                "component_key": source.component_key,
                "installed_version": source.installed_version,
                "source_type": source.source_type,
            }
            for source in template_sources
        ],
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
    db: AsyncSession,
    workflow_id: str,
    entity_id: str,
    *,
    require_valid_steps: bool = False,
    **kwargs,
) -> WorkflowDefinition | None:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return None
    imported_source = _imported_workflow_source(wf)
    if require_valid_steps and kwargs.get("steps") is not None:
        _require_runtime_workflow(
            wf,
            steps=kwargs["steps"],
            imported_source=imported_source,
        )
    steps_changed = (
        kwargs.get("steps") is not None
        and kwargs["steps"] != (wf.steps or [])
    )
    if kwargs.get("steps") is not None:
        await lock_reusable_resource_payload_reference_delta(
            db,
            entity_id=entity_id,
            before=wf.steps,
            after=kwargs["steps"],
        )
    content_patch: dict = {}
    for key, value in kwargs.items():
        if value is not None and hasattr(wf, key):
            if key == "tags":
                import_tags = [
                    tag
                    for tag in (wf.tags or [])
                    if str(tag).strip().lower().startswith(
                        (
                            WORKFLOW_IMPORT_SOURCE_TAG_PREFIX,
                            WORKFLOW_IMPORT_PROOF_TAG_PREFIX,
                        )
                    )
                ]
                value = [*_public_workflow_tags(value), *import_tags]
            if (
                key in _WORKFLOW_CONTENT_REVISION_FIELDS
                and getattr(wf, key) != value
            ):
                content_patch[key] = value
            setattr(wf, key, value)
    if kwargs:
        wf.version = int(wf.version or 1) + 1
    if imported_source:
        public_tags = _public_workflow_tags(wf.tags)
        provenance_tags = (
            workflow_import_provenance_tags(wf, imported_source)
            if not steps_changed
            else [f"{WORKFLOW_IMPORT_SOURCE_TAG_PREFIX}{imported_source}"]
        )
        wf.tags = [*public_tags, *provenance_tags]
    if content_patch:
        # M11: real content changes bump the config revision + audit row.
        from packages.core.revisions import bump_revision

        await bump_revision(db, wf, patch=content_patch)
    await db.flush()
    await db.refresh(wf)
    return wf


async def workflow_definition_references(
    db: AsyncSession,
    workflow_id: str,
    entity_id: str,
) -> dict[str, list[str]]:
    binding_ids = list((await db.execute(
        select(WorkflowBinding.id).where(
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workflow_id == workflow_id,
        )
    )).scalars())

    from packages.core.models.scheduler import ScheduledJob

    scheduled_jobs = list((await db.execute(
        select(ScheduledJob).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.execution_type == "workflow",
        )
    )).scalars())
    scheduled_job_ids = [
        job.id
        for job in scheduled_jobs
        if str((job.execution_target or {}).get("workflow_id") or "") == workflow_id
    ]
    return {
        "binding_ids": sorted(binding_ids),
        "scheduled_job_ids": sorted(scheduled_job_ids),
    }


async def delete_workflow(db: AsyncSession, workflow_id: str, entity_id: str) -> bool:
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        return False
    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_WORKFLOW,
        resource_id=workflow_id,
    )
    references = await workflow_definition_references(
        db,
        workflow_id,
        entity_id,
    )
    if references["binding_ids"] or references["scheduled_job_ids"]:
        raise WorkflowInUseError(**references)
    sources = list((await db.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.workflow_id == workflow_id,
        )
    )).scalars().all())
    for source in sources:
        await db.delete(source)
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

    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_WORKFLOW,
        resource_id=workflow_id,
    )
    await _lock_workspace_workflow_configuration_scope(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
    )
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
    workflow_ids: set[str] = set()
    if (
        "workflow_id" in changes
        and changes["workflow_id"] is not None
        and str(changes["workflow_id"]) != str(binding.workflow_id)
    ):
        workflow_ids.add(str(changes["workflow_id"]))
    if "workspace_id" in changes:
        await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    if workflow_ids:
        await lock_reusable_resource_references(
            db,
            entity_id=entity_id,
            workflow_ids=workflow_ids,
        )
    if "workspace_id" in changes:
        await _lock_workspace_workflow_configuration_scope(
            db,
            workspace_id=changes.get("workspace_id"),
            entity_id=entity_id,
        )
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


class ScheduledWorkflowInputsError(ValueError):
    """A cron trigger has no one to supply the workflow's required inputs."""

    def __init__(self, labels: list[str]):
        self.labels = labels
        super().__init__(
            "A schedule can't supply required inputs ("
            + ", ".join(labels)
            + "). Give them default values on the start node, or run this "
            "workflow manually or from a Workspace instead."
        )


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
    from packages.core.services.workspace_workflow_router import (
        required_workflow_run_inputs_without_default,
    )

    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        raise ValueError("Workflow not found")
    missing = required_workflow_run_inputs_without_default(wf)
    if missing:
        raise ScheduledWorkflowInputsError(missing)

    job = await create_scheduled_job(
        db, entity_id,
        job_id=f"wf-{generate_ulid()}",
        name=name or "Scheduled workflow",
        schedule_kind="cron",
        cron_expr=str(cron),
        timezone_str=timezone_str,
        execution_type="workflow",
        execution_target={
            "workflow_id": workflow_id,
            **({"workspace_id": workspace_id} if workspace_id else {}),
        },
        workspace_id=workspace_id,
        user_id=created_by,
    )
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
        import_source=report.source_tool,
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


def _declared_step_targets(step: dict) -> list[str]:
    """Return every declared target for retry ancestor discovery only."""
    targets: list[str] = []
    for key in ("next", "true_next", "false_next"):
        value = step.get(key) or []
        targets.extend(value if isinstance(value, list) else [value])
    if step.get("type") == "switch":
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        for case in config.get("cases") or []:
            if not isinstance(case, dict):
                continue
            value = case.get("next") or []
            targets.extend(value if isinstance(value, list) else [value])
        value = config.get("default_next") or []
        targets.extend(value if isinstance(value, list) else [value])
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    value = config.get("error_next") or []
    targets.extend(value if isinstance(value, list) else [value])
    return list(dict.fromkeys(str(item) for item in targets if item))


def _selected_step_targets(step: dict, result: dict | None) -> list[str]:
    if result is not None and "next_override" in result:
        value = result.get("next_override")
        if value in (None, ""):
            return []
        return [str(item) for item in (value if isinstance(value, list) else [value])]
    if result is not None and step.get("type") == "condition":
        condition_result = result.get("condition_result", result.get("output"))
        if isinstance(condition_result, bool):
            key = "true_next" if condition_result else "false_next"
            value = step.get(key, step.get("next", []) if condition_result else [])
            return [str(item) for item in (value if isinstance(value, list) else [value])]
        return _declared_step_targets(step)
    if result is not None:
        value = step.get("next") or []
        return [str(item) for item in (value if isinstance(value, list) else [value])]

    # A checkpoint retry can target a node beyond an intermediate condition
    # that never persisted a result (for example, when the process restarted
    # immediately after its producer completed).  Follow every declared branch
    # while discovering ancestors; filtering below still inherits only steps
    # with completed receipts.  Using only ``next`` here loses ``true_next`` in
    # normalized graphs where ``next`` contains the fallback branch.
    return _declared_step_targets(step)


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


def _is_declarative_workflow_schema(value: object) -> bool:
    return (
        isinstance(value, dict)
        and "type" in value
        and any(key in value for key in ("properties", "items", "required"))
    )


def _retry_variables(
    workflow: WorkflowDefinition,
    prior: WorkflowRun,
    inherited_step_ids: set[str],
    variables: dict | None,
    retry_from_step_id: str | None = None,
    *,
    retry_step_ids: set[str] | None = None,
) -> dict:
    prior_variables = prior.variables or {}
    definition_variables = getattr(workflow, "variables", None) or {}
    updated = deepcopy(definition_variables)
    updated.update(deepcopy(prior_variables))
    for key, value in definition_variables.items():
        if _is_declarative_workflow_schema(value):
            updated[key] = deepcopy(value)
    declared_input_roots: set[str] = set()
    for step in workflow.steps or []:
        if step.get("type") not in {"trigger", "webhook"}:
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        for item in config.get("run_inputs") or []:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or item.get("key") or "").strip()
            root = target.split(".", 1)[0]
            if root:
                declared_input_roots.add(root)
    preserved_input_values = {
        root: deepcopy(prior_variables[root])
        for root in declared_input_roots
        if root in prior_variables
    }
    prior_results = prior.step_results or {}
    stage_execution = (
        updated.get("__stage_execution")
        if isinstance(updated.get("__stage_execution"), dict)
        else {}
    )
    retry_stage_ids = retry_step_ids if retry_step_ids is not None else {str(retry_from_step_id or "").strip()}
    retained_stage_states = {
        sid: state for sid, state in stage_execution.items()
        if sid in retry_stage_ids and isinstance(state, dict)
        and (prior_results.get(sid) or {}).get("status") != "completed"
        and state.get("status") != "completed"
    }
    if retained_stage_states:
        updated["__stage_execution"] = deepcopy(retained_stage_states)
    else:
        updated.pop("__stage_execution", None)

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
                or operation_result.get("status") == "invalidated"
                or operation_result.get("continued") is True
            )
        ):
            for key in config.get("set") or {}:
                updated.pop(str(key), None)
    missing_output = object()

    def resolve_inherited_named_output(
        operation_id: str,
        operation_output: object,
        raw: object,
    ) -> object:
        if raw in (None, ""):
            return operation_output
        text = str(raw).strip()
        if not (text.startswith("{{") and text.endswith("}}")):
            return raw
        parts = [part for part in text[2:-2].strip().split(".") if part]
        if not parts:
            return missing_output
        if parts[0] == operation_id:
            current = operation_output
            parts = parts[1:]
        elif isinstance(operation_output, dict) and parts[0] in operation_output:
            current = operation_output
        else:
            current = prior_variables.get(parts[0], missing_output)
            parts = parts[1:]
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part, missing_output)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if index < len(current) else missing_output
            else:
                return missing_output
            if current is missing_output:
                return missing_output
        return current

    for step in workflow.steps or []:
        step_id = str(step.get("id") or "")
        if not step_id or step_id in inherited_step_ids:
            continue
        step_result = prior_results.get(step_id)
        if step_result is None and step.get("type") != "stage":
            # A node that never ran (an unselected branch) never wrote its
            # outputs; the value under a shared name belongs to its sibling.
            continue
        if step.get("type") == "stage" and isinstance(step_result, dict):
            step_result = {
                key: value for key, value in step_result.items()
                if key != "output"
            }
        remove_step_outputs(step, step_result)
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step.get("type") != "stage":
            continue
        retained_stage_state = retained_stage_states.get(step_id)
        stage_state = retained_stage_state if step_id in retry_stage_ids else stage_execution.get(step_id)
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
                isinstance(retained_stage_state, dict)
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
        if (result.get("status") == "completed" or result.get("continued") is True) and "output" in result:
            step_output = deepcopy(result["output"])
            updated[step_id] = step_output
            if output_var:
                updated[output_var] = deepcopy(step_output)
            if step.get("type") == "transform" and isinstance(step_output, dict):
                # A Set transform's receipt is the variable bag it produced.
                for key in config.get("set") or {}:
                    if str(key) in step_output:
                        updated[str(key)] = deepcopy(step_output[str(key)])
            for item in config.get("outputs") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or item.get("name") or "").strip()
                if not key:
                    continue
                value = resolve_inherited_named_output(
                    step_id,
                    step_output,
                    item.get("value"),
                )
                if value is not missing_output:
                    updated[key] = deepcopy(value)
        if step.get("type") != "stage":
            continue
        inherited_stage_state = stage_execution.get(step_id)
        operation_results = (
            inherited_stage_state.get("operation_results")
            if isinstance(inherited_stage_state, dict)
            and isinstance(inherited_stage_state.get("operation_results"), dict)
            else {}
        )
        for operation in config.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("id") or "")
            operation_result = operation_results.get(operation_id) or {}
            operation_output = prior_variables.get(operation_id, missing_output)
            operation_completed = (
                operation_result.get("status") == "completed"
                or operation_result.get("continued") is True
                or (
                    result.get("status") == "completed"
                    and operation_output is not missing_output
                )
            )
            if not operation_completed:
                continue
            operation_config = (
                operation.get("config")
                if isinstance(operation.get("config"), dict)
                else {}
            )
            if operation_output is missing_output:
                continue
            updated[operation_id] = deepcopy(operation_output)
            operation_output_var = str(
                operation_config.get("output_var") or ""
            ).strip()
            if operation_output_var:
                updated[operation_output_var] = deepcopy(operation_output)
            if operation.get("type") == "transform" and isinstance(
                operation_output, dict
            ):
                for key in operation_config.get("set") or {}:
                    if str(key) in operation_output:
                        updated[str(key)] = deepcopy(operation_output[str(key)])
            for item in operation_config.get("outputs") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or item.get("name") or "").strip()
                if not key:
                    continue
                value = resolve_inherited_named_output(
                    operation_id,
                    operation_output,
                    item.get("value"),
                )
                if value is not missing_output:
                    updated[key] = deepcopy(value)
    updated.pop("__result", None)
    updated.update(preserved_input_values)
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
        description=getattr(workflow, "description", "") or "",
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
    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_WORKFLOW,
        resource_id=workflow_id,
    )
    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        raise ValueError("Workflow not found")
    if not wf.is_active or wf.status != "active":
        raise ValueError("Workflow is inactive")
    await _assert_workspace_workflow_execution_ready(
        db,
        workspace_id=workspace_id,
        entity_id=entity_id,
        trigger_source=trigger_source,
    )
    await _validate_workspace_service_dependencies(
        db, workflow=wf, workspace_id=workspace_id,
    )
    if trigger_source in DIRECT_RUN_INPUT_CHECKED_SOURCES:
        # The Run dialog enforces these client-side; API, MCP and scheduled
        # starts must not reach paid nodes with a required input missing.
        from packages.core.services.workspace_workflow_router import (
            check_declared_workflow_run_inputs,
        )

        check_declared_workflow_run_inputs(
            wf, variables=variables, trigger_data=trigger_data,
        )

    # Merge workflow-level default variables with runtime overrides
    merged_vars = dict(wf.variables or {})
    if variables:
        merged_vars.update(variables)
    from packages.core.services.workspace_workflow_router import (
        apply_declared_run_input_values,
    )

    merged_vars = apply_declared_run_input_values(
        wf, merged_vars, trigger_data, explicit_keys=(variables or {}).keys(),
    )

    steps = wf.steps or []
    first_step_id = require_entry_step_id(steps)
    _require_runtime_workflow(wf)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)
    if started_by and trigger_source in MANUAL_WORKFLOW_TRIGGER_SOURCES:
        attempt_trigger_data["_workspace_user_request"] = {"user_id": started_by}

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
        execution_snapshot=build_execution_snapshot(
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
    user_requested: bool = False,
) -> WorkflowRun:
    """Start a run from a binding, resolving its run context.

    The run carries the binding's ``workspace_id`` / ``binding_id`` /
    ``trigger_source`` so the engine resolves the right workspace connectors,
    RAG, approvers and budget (see docs/design/workflow-engine.md §4.1).
    """
    await lock_reusable_resource_reference(
        db,
        entity_id=binding.entity_id,
        resource_type=RESOURCE_WORKFLOW,
        resource_id=binding.workflow_id,
    )
    wf = await get_workflow(db, binding.workflow_id, binding.entity_id)
    if not wf:
        raise ValueError("Workflow not found for binding")
    manual_binding_run = trigger_source == "manual" and bool(started_by)
    if (not binding.enabled or binding.status != "active") and not (
        manual_binding_run and binding.status in {"active", "paused"}
    ):
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
    await _assert_workspace_workflow_execution_ready(
        db,
        workspace_id=effective_workspace_id,
        entity_id=binding.entity_id,
        trigger_source=trigger_source or binding.trigger_type,
        user_requested=user_requested,
    )
    await _validate_workspace_service_dependencies(
        db, workflow=wf, workspace_id=effective_workspace_id,
    )

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
    _require_runtime_workflow(wf)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)
    if started_by and (
        user_requested or (trigger_source or binding.trigger_type) in MANUAL_WORKFLOW_TRIGGER_SOURCES
    ):
        # Only the trusted launch argument can stamp manual admission; request
        # payloads cannot forge this reserved continuation provenance.
        attempt_trigger_data["_workspace_user_request"] = {"user_id": started_by}

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
        execution_snapshot=build_execution_snapshot(
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
    require_workspace_match: bool = False,
    trigger_data: dict | None = None,
    started_by: str | None = None,
) -> list[WorkflowRun]:
    """Fire a trigger: start a run for every enabled binding that matches.

    Matching:
      - binding.enabled is True
      - binding.trigger_type == trigger_type
      - if workspace_id given, binding.workspace_id == workspace_id
      - if require_workspace_match, ``None`` also means only entity-level bindings
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
    if require_workspace_match or workspace_id is not None:
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
    expected_latest_run_id: str | None = None,
    owner_run_id: str | None = None,
    expected_owner_run_id: str | None = None,
    expected_owner_status: str | None = None,
) -> WorkflowRun:
    """Create a linked attempt that resumes from a prior run checkpoint.

    ``expected_latest_run_id`` lets internal continuations atomically refuse to
    replace a newer family attempt or revive one explicitly cancelled after a
    crash. Interactive retries retain their existing cancelled-attempt policy.
    """
    prior = await get_run(db, run_id, entity_id)
    if prior is None:
        raise ValueError("Workflow run not found")
    owner = None
    if owner_run_id is not None:
        owner = await get_run(db, owner_run_id, entity_id)
        if owner is None:
            raise ValueError("Workflow retry owner not found")
        if owner.workspace_id != prior.workspace_id:
            raise ValueError("Workflow retry owner is outside the target workspace")
    user_requested = expected_latest_run_id is None or workflow_run_is_user_requested(prior)
    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_WORKFLOW,
        resource_id=prior.workflow_id,
    )
    if prior.workspace_id:
        await _assert_workspace_workflow_execution_ready(
            db,
            workspace_id=prior.workspace_id,
            entity_id=entity_id,
            trigger_source=(
                prior.trigger_source if expected_latest_run_id is not None else "manual"
            ),
            user_requested=user_requested,
        )
    if owner is not None:
        latest_owner_attempt = await latest_workflow_run_family_attempt(
            db,
            owner,
            lock_root=True,
        )
        await db.refresh(latest_owner_attempt)
        if (
            latest_owner_attempt.id != expected_owner_run_id
            or latest_owner_attempt.status != expected_owner_status
        ):
            raise ValueError("A newer workflow attempt already exists")
    latest_family_attempt = await latest_workflow_run_family_attempt(
        db,
        prior,
        lock_root=True,
    )
    # The selected row may already exist in this Session's identity map from
    # before another transaction cancelled it. Reload it while the family lock
    # is held so an internal continuation cannot revive stale failed state.
    await db.refresh(latest_family_attempt)
    if (
        expected_latest_run_id is not None
        and (
            latest_family_attempt.id != expected_latest_run_id
            or latest_family_attempt.status != WorkflowRunStatus.FAILED
        )
    ):
        raise ValueError("A newer workflow attempt already exists")
    if latest_family_attempt.id != prior.id and latest_family_attempt.status != "cancelled":
        raise ValueError("A newer workflow attempt already exists")
    business_outcome, retry_state = await _retry_business_state(db, prior)
    retryable_business_outcome = (
        prior.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    )
    if prior.status not in {"failed", "cancelled"} and not retryable_business_outcome:
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
    prior_execution_snapshot = execution_snapshot_for_run(workflow, prior)
    if prior_execution_snapshot is None:
        if not stored_fingerprint or stored_fingerprint != current_fingerprint:
            raise ValueError(
                "Workflow definition changed since the failed attempt; this legacy "
                "run has no immutable execution snapshot"
            )
        execution_workflow = workflow
        prior_execution_snapshot = build_execution_snapshot(
            workflow,
            fingerprint=current_fingerprint,
        )
    else:
        execution_workflow = workflow_execution_view(workflow, prior)

    workflow_steps = {
        str(step.get("id") or ""): step
        for step in execution_workflow.steps or []
        if isinstance(step, dict)
    }

    def agent_recovery_checkpoint(step_id: str, result: dict) -> bool:
        step = workflow_steps.get(step_id) or {}
        return (
            step.get("type") == "agent"
            and result.get("producer_completed") is not True
            and isinstance(result.get("chrome_outcome"), dict)
        )

    blocked_retry = next(
        (
            (str(step_id), result)
            for step_id, result in (prior.step_results or {}).items()
            if isinstance(result, dict)
            and result.get("retry_blocked") is True
            and not agent_recovery_checkpoint(str(step_id), result)
        ),
        None,
    )
    if blocked_retry is not None:
        blocked_step_id, blocked_result = blocked_retry
        if blocked_result.get("producer_completed") is True:
            raise ValueError(
                f"Workflow retry is blocked: producer already completed at step "
                f"'{blocked_step_id}', but its output failed the contract"
            )
        raise ValueError(
            f"Workflow retry is blocked at step '{blocked_step_id}': its external "
            "effect may already have executed and must be reconciled first"
        )

    _require_runtime_workflow(
        workflow,
        steps=execution_workflow.steps or [],
        imported_source=prior_execution_snapshot.get("import_source"),
    )
    await _validate_workspace_service_dependencies(
        db,
        workflow=execution_workflow,
        workspace_id=prior.workspace_id,
    )

    normalized_variables = _validate_retry_variable_patch(
        execution_workflow,
        prior,
        variables,
    )
    retry_step_id = retry_target(execution_workflow.steps or [], prior, retry_state, from_step_id)
    step_ids = {
        str(step.get("id"))
        for step in execution_workflow.steps or []
        if step.get("id")
    }
    if retry_step_id not in step_ids:
        raise ValueError("Retry step is not part of the workflow")

    evidence_runs = [prior]
    seen_evidence_ids = {prior.id}
    # New attempts carry a complete recovery frontier. Hydrate historical
    # ancestor-only retries once, without rewriting their immutable records.
    if RETRY_CONTEXT_KEY not in (prior.trigger_data or {}):
        while evidence_runs[-1].effective_retry_of_run_id:
            ancestor_id = evidence_runs[-1].effective_retry_of_run_id
            if ancestor_id in seen_evidence_ids or len(evidence_runs) >= RUN_FAMILY_MAX_RUNS:
                raise ValueError("Workflow retry history is cyclic or exceeds the recovery limit")
            ancestor = await db.scalar(select(WorkflowRun).where(
                WorkflowRun.id == ancestor_id,
                WorkflowRun.entity_id == prior.entity_id,
                WorkflowRun.workflow_id == prior.workflow_id,
                WorkflowRun.workspace_id == prior.workspace_id,
            ))
            if ancestor is None:
                raise ValueError("Workflow retry history is incomplete")
            snapshot = execution_snapshot_for_run(workflow, ancestor)
            if snapshot is None or snapshot["fingerprint"] != prior_execution_snapshot["fingerprint"]:
                raise ValueError("Workflow retry history has a different or missing execution snapshot")
            if (ancestor.lineage_root_run_id or ancestor.id) != (prior.lineage_root_run_id or prior.id):
                raise ValueError("Workflow retry history is outside the current lineage")
            seen_evidence_ids.add(ancestor.id)
            evidence_runs.append(ancestor)
    retry_evidence = merge_retry_evidence(execution_workflow.steps or [], list(reversed(evidence_runs)))
    # Recovery cannot erase an older uncertain external effect merely because
    # a broken intermediate attempt omitted that node's receipt.
    for step_id, result in retry_evidence.executed_results.items():
        if result.get("retry_blocked") is True and not agent_recovery_checkpoint(step_id, result):
            raise ValueError(f"Workflow retry is blocked at step '{step_id}': its prior effect must be reconciled first")
    retry_context = build_retry_context(
        execution_workflow.steps or [], retry_evidence.step_results, retry_step_id,
        explicit_checkpoint=bool(from_step_id),
    )
    inherited_ids = set(retry_context["inherited_step_ids"])
    if normalized_variables:
        # Do not newly reuse parallel outputs across changed inputs. Existing
        # explicit upstream checkpoint semantics remain authoritative.
        ancestors = retry_inherited_step_ids(execution_workflow.steps or [], prior.step_results or {}, retry_step_id)
        if inherited_ids - ancestors:
            raise ValueError("Retry with changed inputs requires an upstream checkpoint before the parallel branches")
    inherited_results = {
        step_id: deepcopy(retry_evidence.step_results[step_id])
        for step_id in inherited_ids
    }
    step_types = {
        str(step.get("id") or ""): str(step.get("type") or "")
        for step in execution_workflow.steps or []
    }
    for recovery_step_id in retry_context["invalidated_step_ids"]:
        prior_retry_result = retry_evidence.executed_results.get(recovery_step_id)
        if (
            step_types.get(recovery_step_id) != "agent"
            or not isinstance(prior_retry_result, dict)
            or prior_retry_result.get("status") == "invalidated"
        ):
            continue
        from packages.core.services.workflow_agent_recovery import agent_retry_context

        # Old attempts predate durable recovery context. Hydrate their actual
        # retry chain once, keeping immutable snapshots and historical results.
        evidence_runs = [prior]
        evidence_result = prior_retry_result
        seen_run_ids = {prior.id}
        while not isinstance(evidence_result.get("agent_recovery"), dict):
            ancestor_id = evidence_runs[-1].effective_retry_of_run_id
            if not ancestor_id or ancestor_id in seen_run_ids:
                break
            if len(evidence_runs) >= RUN_FAMILY_MAX_RUNS:
                raise ValueError("Workflow agent retry history exceeds the recovery limit")
            ancestor = await db.scalar(select(WorkflowRun).where(
                WorkflowRun.id == ancestor_id,
                WorkflowRun.entity_id == prior.entity_id,
                WorkflowRun.workflow_id == prior.workflow_id,
                WorkflowRun.workspace_id == prior.workspace_id,
            ))
            if ancestor is None:
                break
            seen_run_ids.add(ancestor.id)
            evidence_runs.append(ancestor)
            evidence_result = (ancestor.step_results or {}).get(recovery_step_id) or {}
        receipts_by_run = {}
        recovery = None
        for evidence_run in reversed(evidence_runs):
            evidence_result = (evidence_run.step_results or {}).get(recovery_step_id)
            if not isinstance(evidence_result, dict):
                continue
            recovery = agent_retry_context(
                {**evidence_result, **({"agent_recovery": recovery} if recovery else {})},
                run_id=evidence_run.id, step_id=recovery_step_id, error=evidence_run.error,
                receipts=receipts_by_run.get(evidence_run.id),
            )
        inherited_results[recovery_step_id] = {
            **deepcopy(prior_retry_result),
            "agent_recovery": recovery,
            "status": "failed",
            "resume_strategy": "reexecute",
            "agent_retry_recovery": True,
        }
    prior_trigger = {
        key: deepcopy(value)
        for key, value in (prior.trigger_data or {}).items()
        if key not in {
            "_workflow_history_summary",
        }
    }
    lineage_root_run_id = prior.lineage_root_run_id or prior.id
    attempt_number = max(
        prior.effective_attempt_number,
        latest_family_attempt.effective_attempt_number,
    ) + 1
    retry_trigger = _attempt_trigger_data(execution_workflow, prior_trigger)
    retry_trigger[RETRY_CONTEXT_KEY] = {
        **retry_context,
        "fingerprint": prior_execution_snapshot["fingerprint"],
        "source_run_ids": {sid: retry_evidence.source_run_ids[sid] for sid in inherited_ids},
    }
    if user_requested and started_by:
        retry_trigger["_workspace_user_request"] = {"user_id": started_by}
    if prior_snapshot:
        retry_trigger.update(_snapshot_display_metadata(prior_snapshot))
    retry_trigger["input_patch"] = deepcopy(variables or {})
    retry_variables = _retry_variables(
        execution_workflow,
        retry_evidence,
        inherited_ids,
        normalized_variables,
        retry_step_id,
        retry_step_ids=set(retry_context["entry_step_ids"]),
    )
    retry_variables = await _refresh_retry_workflow_project(
        db,
        prior,
        retry_variables,
    )

    retry_execution_snapshot = deepcopy(prior_execution_snapshot)

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
                execution_workflow,
                fingerprint=prior_execution_snapshot["fingerprint"],
            )
        ),
        execution_snapshot=retry_execution_snapshot,
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
    top_level_only: bool = False,
) -> list[WorkflowRun]:
    q = select(WorkflowRun).where(
        WorkflowRun.entity_id == entity_id,
        _NOT_A_NODE_TEST,
    )
    if summary:
        q = q.options(*_run_summary_options())
    if top_level_only:
        lineage_root = aliased(WorkflowRun)
        nested_lineage = select(lineage_root.id).where(
            lineage_root.id == func.coalesce(
                WorkflowRun.lineage_root_run_id,
                WorkflowRun.id,
            ),
            lineage_root.entity_id == WorkflowRun.entity_id,
            lineage_root.trigger_data["parent_run_id"].as_string().is_not(None),
        ).exists()
        q = q.where(~nested_lineage)
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


async def query_runs(
    db: AsyncSession,
    entity_id: str,
    *,
    workflow_ids: Sequence[str] | None = None,
    workspace_ids: Sequence[str] | None = None,
    binding_ids: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    trigger_sources: Sequence[str] | None = None,
    started_by_ids: Sequence[str] | None = None,
    readable_workspace_ids: set[str] | None = None,
    created_after: str | datetime | None = None,
    created_before: str | datetime | None = None,
    updated_after: str | datetime | None = None,
    updated_before: str | datetime | None = None,
    started_after: str | datetime | None = None,
    started_before: str | datetime | None = None,
    completed_after: str | datetime | None = None,
    completed_before: str | datetime | None = None,
    attempt_number_min: int | None = None,
    attempt_number_max: int | None = None,
    summary: bool = False,
    limit: int | None = 50,
    offset: int = 0,
) -> tuple[list[WorkflowRun], int]:
    """Query runs with all predicates applied before deterministic pagination."""
    conditions = [WorkflowRun.entity_id == entity_id]
    if readable_workspace_ids is not None:
        conditions.append(or_(
            WorkflowRun.workspace_id.is_(None),
            WorkflowRun.workspace_id.in_(readable_workspace_ids),
        ))
    for column, values, lowercase in (
        (WorkflowRun.workflow_id, workflow_ids, False),
        (WorkflowRun.workspace_id, workspace_ids, False),
        (WorkflowRun.binding_id, binding_ids, False),
        (WorkflowRun.status, statuses, True),
        (WorkflowRun.trigger_source, trigger_sources, True),
        (WorkflowRun.started_by, started_by_ids, False),
    ):
        normalized = _workflow_query_values(values, lowercase=lowercase)
        if normalized:
            expression = func.lower(column) if lowercase else column
            conditions.append(expression.in_(normalized))
    _append_workflow_numeric_range(
        conditions,
        WorkflowRun.attempt_number,
        attempt_number_min,
        attempt_number_max,
        "attempt_number",
    )
    for column, after, before, field_name in (
        (WorkflowRun.created_at, created_after, created_before, "created"),
        (WorkflowRun.updated_at, updated_after, updated_before, "updated"),
        (WorkflowRun.started_at, started_after, started_before, "started"),
        (WorkflowRun.completed_at, completed_after, completed_before, "completed"),
    ):
        _append_workflow_datetime_range(
            conditions,
            column,
            after,
            before,
            field_name,
        )
    conditions.append(_NOT_A_NODE_TEST)
    page = select(WorkflowRun).where(*conditions)
    if summary:
        page = page.options(*_run_summary_options())
    page = page.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
    if limit is not None:
        page = page.limit(limit).offset(max(0, offset))
    elif offset:
        page = page.offset(max(0, offset))
    count_query = select(func.count()).select_from(WorkflowRun).where(*conditions)
    rows = list((await db.execute(page)).scalars().all())
    total = int((await db.execute(count_query)).scalar_one())
    return rows, total


def _workflow_query_values(
    values: Sequence[object] | None,
    *,
    lowercase: bool = False,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if lowercase:
            text = text.lower()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _workflow_datetime_bound(
    value: str | datetime | None,
    field_name: str,
) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{field_name} range endpoints must be ISO-8601 datetimes"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _append_workflow_datetime_range(
    conditions: list,
    column: object,
    after: str | datetime | None,
    before: str | datetime | None,
    field_name: str,
) -> None:
    after_dt = _workflow_datetime_bound(after, field_name)
    before_dt = _workflow_datetime_bound(before, field_name)
    if after_dt is not None and before_dt is not None and after_dt > before_dt:
        raise ValueError(
            f"{field_name}_after must be less than or equal to {field_name}_before"
        )
    if after_dt is not None:
        conditions.append(column >= after_dt)
    if before_dt is not None:
        conditions.append(column <= before_dt)


def _append_workflow_numeric_range(
    conditions: list,
    column: object,
    minimum: int | None,
    maximum: int | None,
    field_name: str,
) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            f"{field_name}_min must be less than or equal to {field_name}_max"
        )
    if minimum is not None:
        conditions.append(column >= minimum)
    if maximum is not None:
        conditions.append(column <= maximum)


# ── Step Execution Engine ──

NODE_TEST_WORKFLOW_ID = "node-test"
NODE_TEST_TRIGGER_SOURCE = "node_test"
# Node tests are not Flow runs; run lists and history leave them out.
_NOT_A_NODE_TEST = or_(
    WorkflowRun.trigger_source.is_(None),
    WorkflowRun.trigger_source != NODE_TEST_TRIGGER_SOURCE,
)


async def execute_node_test(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    step: dict,
    variables: dict | None = None,
) -> dict:
    """Run one node on its own ("Test node") as a real, short-lived run.

    Tools a node calls check that their Workflow run exists, is running, and
    belongs to the caller. An in-memory stand-in run failed that check, so
    every tool-using node (image, video, tool calls) could not be tested. The
    test run is committed before the node runs, marked terminal afterwards,
    and filed under its own id so it never shows in a Flow's history.
    """
    from packages.core.ai.workflow_runner import WorkflowRunner

    run_id = generate_ulid()
    run = WorkflowRun(
        id=run_id,
        workflow_id=NODE_TEST_WORKFLOW_ID,
        entity_id=entity_id,
        lineage_root_run_id=run_id,
        lineage_is_legacy=False,
        attempt_number=1,
        status="running",
        current_step_id=str(step.get("id") or "") or None,
        variables=dict(variables or {}),
        step_results={},
        trigger_data={},
        trigger_source=NODE_TEST_TRIGGER_SOURCE,
        started_by=user_id or None,
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.commit()
    try:
        result = await WorkflowRunner()._execute_step_safe(step, run, db)
    finally:
        await db.refresh(run)
        if run.status == "running":
            run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
    if result.get("status") == "failed":
        run.status = "failed"
        run.error = summarize_trace_text(result.get("error"), fallback="Node test failed")
        await db.commit()
    return result


async def execute_workflow_step(db: AsyncSession, run_id: str, entity_id: str) -> dict:
    """Execute one ready node through the canonical workflow runner.

    The manual ``/step`` API used to duplicate a small subset of runner logic,
    which made newer nodes (code, HTTP, media, loop, subworkflow, etc.) appear to
    succeed without executing. Reusing ``WorkflowRunner`` keeps full-run,
    single-node, and step-by-step behavior identical. This function owns the
    commit so the execution claim is held until the node checkpoint is durable.
    """
    from packages.core.ai.workflow_runner import (
        WorkflowRunner,
        mark_workflow_terminal_effects_pending,
    )
    from packages.core.services.workflow_run_execution_claim import (
        WorkflowRunExecutionClaimLost,
        commit_fenced_execution_boundary,
        workflow_run_execution_claim,
    )

    try:
        async with workflow_run_execution_claim(run_id) as claim:
            if not claim:
                return {"error": "Run is already executing"}
            result = await _execute_claimed_workflow_step(
                db,
                run_id,
                entity_id,
            )
            run = await get_run(db, run_id, entity_id)
            settled_statuses = {
                "completed",
                "failed",
                "paused",
                "cancelled",
            }
            if (
                run is not None
                and run.status in settled_statuses
                and result.get("status") in settled_statuses
            ):
                from packages.core.ledger.adapters import record_workflow_run_status
                from packages.core.services.workflow_chat_projection import (
                    project_workflow_run_status,
                )

                await project_workflow_run_status(db, run=run)
                if run.status == WorkflowRunStatus.PAUSED:
                    await record_workflow_run_status(db, run)
                else:
                    mark_workflow_terminal_effects_pending(run)
            await commit_fenced_execution_boundary(
                db.commit,
                before_commit=claim.raise_if_lost,
                after_commit=claim.mark_terminal_committed,
                execution_claim=claim,
                session=db,
            )
            if (
                run is not None
                and run.status in WORKFLOW_RUN_TERMINAL_STATUSES
                and result.get("status") in WORKFLOW_RUN_TERMINAL_STATUSES
            ):
                from packages.core.ai.workflow_runner import (
                    finalize_workflow_terminal_effects_best_effort,
                )

                await finalize_workflow_terminal_effects_best_effort(
                    run,
                    db,
                    context="manual workflow step",
                )
            return result
    except WorkflowRunExecutionClaimLost:
        # A canceled asyncpg operation can leave the request transaction
        # unusable. Roll it back before settling the run through a fresh
        # session, matching the full-run fail-closed path.
        await db.rollback()
        settled_status = await WorkflowRunner._fail_lost_execution_claim(run_id)
        if settled_status is None:
            return {
                "status": "running",
                "error": "Workflow execution ownership changed; the current owner is still running",
            }
        return {
            "status": settled_status,
            "error": (
                "Workflow execution claim was lost"
                if settled_status == "failed"
                else None
            ),
        }


async def _execute_claimed_workflow_step(
    db: AsyncSession,
    run_id: str,
    entity_id: str,
) -> dict:
    """Execute one node while ``execute_workflow_step`` holds the run claim."""
    run = await get_run(db, run_id, entity_id)
    if not run or run.status not in ("running", "pending"):
        return {"error": "Run not active"}

    workflow = await get_workflow(db, run.workflow_id, entity_id)
    execution_workflow = workflow_execution_view(workflow, run)
    if not execution_workflow:
        return {"error": "Workflow not found"}
    if workflow is not None and workflow_definition_changed(workflow, run):
        run.status = "failed"
        run.error = DEFINITION_CHANGED_ERROR
        run.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return {"status": "failed", "error": DEFINITION_CHANGED_ERROR}

    from packages.core.ai.workflow_runner import (
        WorkflowRunner,
        _continue_failed_result,
        apply_workflow_approval_revision,
    )

    runner = WorkflowRunner()
    # Same scheduling as the full runner: a revise decision rewinds first.
    apply_workflow_approval_revision(execution_workflow.steps or [], run)
    runnable = runner._find_runnable_steps(execution_workflow, run)
    if not runnable:
        if runner._all_steps_done(execution_workflow, run):
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return {"status": "completed"}
        return {"error": "No runnable step"}

    step = runnable[0]
    append_execution_trace(run, node=step, status="running")
    result = await runner._execute_step_safe(step, run, db)
    _continue_failed_result(step, result, run)
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
        next_steps = runner._find_runnable_steps(execution_workflow, run)
        if next_steps:
            run.current_step_id = next_steps[0]["id"]
        elif runner._all_steps_done(execution_workflow, run):
            run.status = "completed"
            run.completed_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(run)
    result["next_step_id"] = run.current_step_id if run.status == "running" else None
    return result
