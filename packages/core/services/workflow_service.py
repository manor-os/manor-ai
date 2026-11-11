"""Workflow service — CRUD for definitions & runs, step execution engine."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, load_only, with_expression

from packages.core.ai.workflow_import import import_workflow
from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.ai.workflow_failure import workflow_run_failure_headline
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
from packages.core.services.workflow_record_source import record_select_config_error
from packages.core.services.viewer_language import (
    RUN_DISPLAY_I18N_KEY,
    binding_name_translations,
    chat_entrypoint_title_translations,
    run_display_translations,
)
from packages.core.services.workspace_translation import schedule_workspace_translation
from packages.core.services.workspace_autonomy import (
    workspace_allows_configured_work,
    workspace_allows_manual_work,
)
from packages.core.workspaces import is_sandbox_workspace
from packages.core.services.workflow_run_trace import (
    WORKFLOW_IMPORT_PROOF_TAG_PREFIX,
    WORKFLOW_IMPORT_SOURCE_TAG_PREFIX,
    DEFINITION_CHANGED_ERROR,
    append_execution_trace,
    build_definition_snapshot,
    build_execution_snapshot,
    execution_snapshot_for_run,
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
    WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD,
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
from packages.core.services.workflow_contracts import workflow_step_label

logger = logging.getLogger(__name__)


SUPPORTED_WORKFLOW_STEP_TYPES = set(CANONICAL_NODE_TYPES) | {"webhook", "note"}
RUN_FAMILY_MAX_RUNS = 200
MANUAL_WORKFLOW_TRIGGER_SOURCES = {
    "manual", "workspace_chat", "global_chat", "agent_test",
    "public_webchat", "site_form",
}
DIRECT_RUN_INPUT_CHECKED_SOURCES = {"manual", "schedule"}
# The App entry a run was started from. It only groups history; retries copy it.
WORKSPACE_APP_ACTION_KEY = "_workspace_app_action"
# Who asked for a retry that continues as the run's starter (see retry_workflow_run).
RETRY_REQUESTED_BY_KEY = "_workflow_retry_requested_by"
# Work finished outside a run that a retry accepted as one step's result.
SUPPLIED_OUTPUT_KEY = "_workflow_supplied_output"
RUN_RESERVED_TRIGGER_FIELDS = {
    RETRY_CONTEXT_KEY,
    RETRY_REQUESTED_BY_KEY,
    "retry_of_run_id",
    "retry_from_step_id",
    "attempt_number",
    "_workflow_history_summary",
    "_workspace_user_request",
    WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD,
    WORKFLOW_DISPATCH_RECOVERY_TRIGGER_FIELD,
    # A public start/binding payload must never mint this server-owned context.
    "_workflow_runtime_context",
    "_workflow_effect_review",
    SUPPLIED_OUTPUT_KEY,
    RUN_DISPLAY_I18N_KEY,
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
    started_by: str | None = None,
    trigger_data: dict | None = None,
    workflow_id: str | None = None,
    binding: WorkflowBinding | None = None,
    existing_configured_origin: bool = False,
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
    if manual_trigger:
        if not workspace_allows_manual_work(workspace):
            raise ValueError("Workflow execution requires an available Workspace")
    elif not workspace_allows_configured_work(workspace):
        raise ValueError("Workflow execution requires an available Workspace")
    elif workspace.status == "paused":
        configured_origin = False
        if trigger_source == "schedule":
            from packages.core.governance.service import (
                scheduled_workflow_admission_is_valid,
                scheduled_workflow_origin_is_valid,
            )

            data = trigger_data if isinstance(trigger_data, dict) else {}
            validator = (
                scheduled_workflow_origin_is_valid
                if existing_configured_origin
                else scheduled_workflow_admission_is_valid
            )
            configured_origin = bool(workflow_id) and await validator(
                db,
                entity_id=entity_id,
                workspace_id=workspace.id,
                scheduled_job_id=data.get("scheduled_job_id"),
                scheduled_run_id=data.get("scheduled_run_id"),
                workflow_id=workflow_id or "",
                binding_id=binding.id if binding is not None else None,
            )
        elif binding is not None:
            configured_origin = bool(
                binding.enabled
                and binding.status == "active"
                and binding.trigger_type == trigger_source
                and binding.trigger_type not in {"manual", "mcp"}
            )
        if not configured_origin:
            raise ValueError("Workflow execution requires an enabled configured Workflow")
        from packages.core.services.workspace_access import user_can_read_workspace_by_identity

        if not started_by or not await user_can_read_workspace_by_identity(
            db,
            workspace=workspace,
            entity_id=entity_id,
            user_id=started_by,
        ):
            raise ValueError("Workflow execution actor cannot access this Workspace")
    if is_sandbox_workspace(workspace):
        raise ValueError("Workspace simulation cannot start an ordinary Workflow run")
    from packages.core.services.workspace_readiness import (
        blocking_setup_blocks_workflow,
        evaluate_workspace_blocking_setup,
    )

    # Integrations are the acting person's own: whoever starts or retries the run brings their connections.
    setup_status = await evaluate_workspace_blocking_setup(db, workspace, actor_user_id=started_by)
    if setup_status is not None and setup_status.blocks_work:
        # Setup a Workflow never reaches (a publishing connection, for a song) does not hold it up.
        definition = await db.get(WorkflowDefinition, workflow_id) if workflow_id else None
        if blocking_setup_blocks_workflow(setup_status, definition.steps if definition is not None else None):
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
            WorkflowRun.summary_display_i18n,
            WorkflowRun.trigger_data[RUN_DISPLAY_I18N_KEY],
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


# The operations the runner's workflow_project node executes, each with the
# config fields it needs. Keep in step with ``_execute_workflow_project_step``.
WORKFLOW_PROJECT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "create": ("project_type", "schema_version", "state_schema"),
    "claim": ("project_type", "project_key", "schema_version", "state_schema"),
    "get": ("project_id", "project_type", "schema_version", "state_schema"),
    "list": ("project_type", "state_schema"),
    "lease_due": ("project_type", "schema_version", "state_schema", "due_path", "pending_stages"),
    "complete_lease": ("project_id", "project_type", "schema_version", "state_schema", "current_stage"),
    "patch": ("project_id", "project_type", "schema_version", "expected_revision", "state_schema"),
    "delete": ("project_id", "project_type"),
}
# What each operation's step output holds, for ``{{step.field}}`` bindings.
_WORKFLOW_PROJECT_RECORD_FIELDS = frozenset({
    "project_id", "created_by", "project_type", "project_key", "schema_version",
    "current_stage", "state", "revision", "last_run_id",
})
_WORKFLOW_PROJECT_OUTPUT_FIELDS: dict[str, frozenset[str]] = {
    **{operation: _WORKFLOW_PROJECT_RECORD_FIELDS for operation in WORKFLOW_PROJECT_REQUIRED_FIELDS},
    "claim": _WORKFLOW_PROJECT_RECORD_FIELDS | {"claimed"},
    "list": frozenset({"projects", "has_more", "next_cursor"}),
    "lease_due": frozenset({"has_project", "project"}),
    "delete": frozenset({"project_id", "deleted_ids", "deleted_count"}),
}
# Config fields the runner resolves to a project identity or revision.
_WORKFLOW_PROJECT_BINDING_FIELDS = ("project_id", "expected_revision", "parent_id", "after_id")
# State writes only a patch applies, with the keys each list entry is read by.
_WORKFLOW_PROJECT_PATCH_WRITES: dict[str, tuple[str, ...]] = {
    "list_appends": ("path", "items"),
    "list_upserts": ("path", "item"),
    # The runner defaults keys/items to [] for clearing or reordering existing rows.
    "list_reconciles": ("path",),
    "list_removes": ("path", "field", "values"),
    "history_event": (),
}
_STEP_REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)[^{}]*\}\}")


def _step_operation(config: dict) -> str:
    return str(config.get("operation") or "").strip().lower()


def _validate_workflow_project_contracts(
    nodes: list,
    errors: list[dict[str, str]],
) -> None:
    """Reject workflow_project nodes the runner cannot execute as written.

    Covers top-level nodes and stage operations: an operation the runner does
    not have, ``state`` on a patch (the runner only applies ``patch``), and an
    identity binding such as ``{{create_article.id}}`` that names a field the
    producing workflow_project step never outputs.
    """

    flat: list[tuple[str, dict]] = []

    def collect(items: object, parent_id: str) -> None:
        if not isinstance(items, list):
            return
        for index, node in enumerate(items):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or index)
            flat.append((f"{parent_id}.{node_id}" if parent_id else node_id, node))
            config = node.get("config") if isinstance(node.get("config"), dict) else {}
            for nested_key in ("operations", "steps"):
                collect(config.get(nested_key), flat[-1][0])

    collect(nodes, "")
    # A name another node also writes (its id, output_var or named output) is
    # ambiguous; such references are left to the runner.
    producers: dict[str, set[str]] = {}
    other_names: set[str] = set()
    for _, node in flat:
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        names = {str(node.get("id") or "").strip(), str(config.get("output_var") or "").strip()}
        names.discard("")
        operation = _step_operation(config)
        if str(node.get("type") or "").strip().lower() == "workflow_project":
            fields = _WORKFLOW_PROJECT_OUTPUT_FIELDS.get(operation)
            for name in names:
                if fields is None:
                    other_names.add(name)
                else:
                    producers.setdefault(name, set()).update(fields)
        else:
            other_names.update(names)
        for item in config.get("outputs") or []:
            if isinstance(item, dict):
                other_names.add(str(item.get("key") or item.get("name") or "").strip())

    supported = ", ".join(WORKFLOW_PROJECT_REQUIRED_FIELDS)
    for qualified_id, node in flat:
        if str(node.get("type") or "").strip().lower() != "workflow_project":
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        operation = _step_operation(config)
        if operation and operation not in WORKFLOW_PROJECT_REQUIRED_FIELDS:
            errors.append({
                "code": "unsupported_workflow_project_operation",
                "message": (
                    f"Node '{qualified_id}' has unsupported workflow_project operation "
                    f"'{operation}'; use one of {supported}. To change an existing project use "
                    "'patch' with project_id, expected_revision (the revision output of the step "
                    "that last read or wrote it) and a 'patch' object of state changes"
                ),
            })
        key = config.get("project_key")
        # One key is one project: a fixed key on create lets only the first run make it.
        if operation == "create" and isinstance(key, str) and key.strip() and "{{" not in key:
            errors.append({
                "code": "invalid_node_config",
                "message": (
                    f"Node '{qualified_id}' creates its project under the fixed project_key "
                    f"'{key.strip()}', so only the first run can create it and every later run fails. "
                    "Leave project_key out so each run makes its own project, or build it from the short "
                    "value that tells runs apart (an id, a code or a period such as '{{period_key}}'); "
                    "to keep reusing one project, claim it instead"
                ),
            })
        if operation == "patch" and "state" in config:
            errors.append({
                "code": "invalid_node_config",
                "message": (
                    f"Node '{qualified_id}' sets config.state, which a workflow_project patch "
                    "ignores; put the state changes in config.patch"
                ),
            })
        stage, allowed = config.get("current_stage"), config.get("allowed_stages")
        # A patch moves the project only to a stage it allows; a run value is checked when it runs.
        if (operation == "patch" and isinstance(stage, str) and stage and "{{" not in stage
                and isinstance(allowed, list) and allowed and stage not in {str(value) for value in allowed}):
            errors.append({
                "code": "invalid_node_config",
                "message": (
                    f"Node '{qualified_id}' moves the project to stage '{stage}', which its "
                    f"config.allowed_stages {allowed} does not include; add it there"
                ),
            })
        for key, entry_keys in _WORKFLOW_PROJECT_PATCH_WRITES.items():
            if key not in config:
                continue
            if operation != "patch":
                errors.append({
                    "code": "invalid_node_config",
                    "message": (
                        f"Node '{qualified_id}' sets config.{key}, which only a workflow_project patch "
                        f"applies; a {operation or 'get'} ignores it. Write it on a patch of the project"
                    ),
                })
                continue
            if not entry_keys:
                continue
            entries = config.get(key) or []
            if not isinstance(entries, list):
                errors.append({
                    "code": "invalid_node_config",
                    "message": f"Node '{qualified_id}' config.{key} must be an array of objects",
                })
                continue
            for entry in entries:
                missing = [name for name in entry_keys if name not in entry] if isinstance(entry, dict) else entry_keys
                if missing:
                    errors.append({
                        "code": "invalid_node_config",
                        "message": (
                            f"Node '{qualified_id}' has a config.{key} entry without "
                            + ", ".join(missing) + f"; each entry is {{{', '.join(entry_keys)}}}"
                            + (", key? (the field that tells items apart, default 'id')" if key != "list_removes" else "")
                        ),
                    })
        for field in _WORKFLOW_PROJECT_BINDING_FIELDS:
            value = config.get(field)
            if not isinstance(value, str):
                continue
            for name, attribute in _STEP_REFERENCE.findall(value):
                outputs = producers.get(name)
                if outputs is None or name in other_names or attribute in outputs:
                    continue
                errors.append({
                    "code": "unknown_workflow_project_output",
                    "message": (
                        f"Node '{qualified_id}' config.{field} reads '{name}.{attribute}', but "
                        f"workflow_project step '{name}' outputs no '{attribute}'; it outputs "
                        + ", ".join(sorted(outputs))
                    ),
                })


def _required_step_config_fields(step_type: str, config: dict) -> tuple[str, ...]:
    operation = _step_operation(config)
    if step_type == "workflow_project":
        return WORKFLOW_PROJECT_REQUIRED_FIELDS.get(operation, ("operation",))
    if step_type == "workflow_action_grant":
        if operation == "create":
            declaration = config.get("declaration")
            if (
                isinstance(declaration, dict)
                and declaration.get("kind") == "youtube_publication_v1"
                and declaration.get("source") == "workflow_execution"
            ):
                return ("declaration",)
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
    if step_type == "workflow_effect_deadline":
        return ("deadline_at", "action_keys")
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


# The operations the runner executes for node types that dispatch on
# ``config.operation``; any other value fails only once a run reaches it.
STEP_OPERATIONS: dict[str, tuple[str, ...]] = {
    "workflow_project": ("create", "claim", "get", "list", "lease_due", "complete_lease", "patch", "delete"),
    "workflow_action_grant": ("create", "revoke", "revoke_current"),
    "browser_effect": ("decide", "transition"),
}
_STEP_OPERATION_HINTS = {
    "workflow_project": (
        " To change a project's state or stage use 'patch' with project_id ('{{create_step.project_id}}'), "
        "expected_revision (the previous project step's '{{step_id.revision}}'), patch:{...}, current_stage "
        "and the state_schema."
    ),
}


def _validate_step_operation(
    node_id: str, node_type: str, config: dict, errors: list[dict[str, str]],
) -> None:
    operations = STEP_OPERATIONS.get(node_type)
    raw = config.get("operation")
    if operations is None or raw in (None, "") or (isinstance(raw, str) and "{{" in raw):
        return
    operation = str(raw).strip().lower()
    if operation not in operations:
        errors.append({
            "code": "unsupported_node_operation",
            "message": (
                f"Node '{node_id}' has unsupported config.operation '{raw}' for {node_type}; use one of "
                + ", ".join(operations) + "." + _STEP_OPERATION_HINTS.get(node_type, "")
            ),
        })


def _validate_step_expectations(
    node_id: str, node_type: str, config: dict, errors: list[dict[str, str]],
) -> None:
    from packages.core.contracts.step_execution import STEP_EXPECTS_KEY, parse_step_expectations

    if STEP_EXPECTS_KEY not in config:
        return
    if node_type != "agent":
        message = f"config.{STEP_EXPECTS_KEY} only applies to agent nodes"
    else:
        try:
            parse_step_expectations(config[STEP_EXPECTS_KEY])
            return
        except ValueError as exc:
            message = f"config.{STEP_EXPECTS_KEY} {exc}"
    errors.append({"code": "invalid_step_expectations", "message": f"Node '{node_id}' {message}"})


def _validate_project_folder(
    node_id: str, node_type: str, config: dict, errors: list[dict[str, str]],
) -> None:
    """A literal ``folder.under`` must name a folder inside ``projects/``; the runner refuses any other."""
    from packages.core.services.workspace_layout import PROJECT_ARTIFACT_SEGMENT

    folder = config.get("folder")
    if node_type != "workflow_project" or not isinstance(folder, dict):
        return
    under = folder.get("under")
    if under in (None, "") or (isinstance(under, str) and "{{" in under):
        return
    parts = str(under).strip().strip("/").split("/")
    if parts[0] != PROJECT_ARTIFACT_SEGMENT or any(part in {"", ".", ".."} for part in parts):
        errors.append({
            "code": "invalid_project_folder",
            "message": (
                f"Node '{node_id}' config.folder.under {under!r} is not inside "
                f"{PROJECT_ARTIFACT_SEGMENT}/. Omit it to keep the project in "
                f"{PROJECT_ARTIFACT_SEGMENT}/<name>, or name a parent project's folder."
            ),
        })


# Scope words a reference may be written under by habit from other tools. The
# runner has no such scopes: run inputs and variables are top-level names and
# step results live under the step id, so ``{{inputs.topic}}`` stays
# unrendered unless the graph itself names something ``inputs``.
_UNDECLARED_SCOPE_ROOTS = frozenset({
    "inputs", "run_inputs", "variables", "vars", "params", "steps", "nodes",
})
_TEMPLATE_REFERENCE = re.compile(r"\{\{([^{}]+?)\}\}")
_REFERENCE_ROOT = re.compile(r"\s*([A-Za-z_][\w-]*)")
# Config fields whose string value names a variable the step writes.
_DECLARING_CONFIG_KEYS = frozenset({"key", "name", "target", "variable", "output_var", "item_var"})
_DECLARING_LISTS = ("run_inputs", "inputs", "outputs", "variables", "assignments")


def _declared_workflow_names(nodes: object, names: set[str]) -> None:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("id", "name"):
            if isinstance(node.get(key), str) and node[key].strip():
                names.add(node[key].strip())
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(node.get("type") or "").strip().lower() == "wait" and config.get("wait_type") in {"input", "human_input"}:
            names.add(str(config.get("response_variable") or "inputs").strip() or "inputs")
        for key, value in config.items():
            if isinstance(value, str) and (key in _DECLARING_CONFIG_KEYS or key.endswith(("_variable", "_var"))):
                names.add(value.strip())
        for list_key in _DECLARING_LISTS:
            items = config.get(list_key)
            if isinstance(items, dict):
                names.update(str(key).strip() for key in items)
            for item in items if isinstance(items, list) else ():
                if isinstance(item, dict):
                    names.update(
                        str(item[key]).strip() for key in ("key", "name", "target")
                        if isinstance(item.get(key), str)
                    )
        for nested_key in ("operations", "steps"):
            _declared_workflow_names(config.get(nested_key), names)


def _template_references(value: object, found: list[str]) -> None:
    if isinstance(value, str):
        found.extend(match.strip() for match in _TEMPLATE_REFERENCE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            _template_references(item, found)
    elif isinstance(value, list):
        for item in value:
            _template_references(item, found)


# What a workflow_project step that returns one project outputs (the
# runner's _workflow_project_output, plus ``claimed`` for claim).
_PROJECT_STEP_OUTPUT_FIELDS = frozenset({
    "project_id", "created_by", "project_type", "project_key", "schema_version",
    "current_stage", "state", "revision", "last_run_id", "claimed",
})
_SINGLE_PROJECT_OPERATIONS = frozenset({"create", "claim", "get", "patch", "complete_lease"})


def _project_step_ids(nodes: object, found: set[str]) -> None:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if (
            str(node.get("type") or "").strip().lower() == "workflow_project"
            and str(config.get("operation") or "").strip().lower() in _SINGLE_PROJECT_OPERATIONS
            and isinstance(node.get("id"), str)
        ):
            found.add(node["id"].strip())
        for nested_key in ("operations", "steps"):
            _project_step_ids(config.get(nested_key), found)


def _validate_workflow_references(
    nodes: list,
    errors: list[dict[str, str]],
    *,
    variable_names: Sequence[str] = (),
) -> None:
    """Refuse references the runner cannot resolve.

    A reference under a scope the runner does not have, or to a field a
    project step never outputs, would stay unrendered at run time.
    """
    declared = {str(name).strip() for name in variable_names}
    _declared_workflow_names(nodes, declared)
    project_steps: set[str] = set()
    _project_step_ids(nodes, project_steps)
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        references: list[str] = []
        _template_references(node.get("config"), references)
        for reference in dict.fromkeys(references):
            match = _REFERENCE_ROOT.match(reference)
            root = match.group(1) if match else ""
            if root in project_steps and root not in variable_names:
                field = reference[match.end():].lstrip(".").split(".")[0].strip()
                if field and field not in _PROJECT_STEP_OUTPUT_FIELDS:
                    errors.append({
                        "code": "unresolvable_reference",
                        "message": (
                            f"Node '{node.get('id') or index}' refers to '{{{{{reference}}}}}', but project step "
                            f"'{root}' outputs " + ", ".join(sorted(_PROJECT_STEP_OUTPUT_FIELDS - {"claimed"}))
                            + f" (project fields are under '{root}.state')."
                        ),
                    })
                continue
            if root not in _UNDECLARED_SCOPE_ROOTS or root in declared:
                continue
            field = reference[match.end():].lstrip(".").strip() or "name"
            errors.append({
                "code": "unresolvable_reference",
                "message": (
                    f"Node '{node.get('id') or index}' refers to '{{{{{reference}}}}}', but nothing in "
                    f"this Workflow is named '{root}'. Refer to a run input or variable by its own "
                    f"name ('{{{{{field.split('.')[0]}}}}}') and to a step result by its step id "
                    "('{{step_id.field}}')."
                ),
            })


_AGENT_OUTPUT_STEP_TYPES = frozenset({"agent", "llm", "classifier"})
# Schema keywords that let an object hold fields its ``properties`` do not list.
_OPEN_SCHEMA_KEYWORDS = ("anyOf", "oneOf", "allOf", "$ref", "patternProperties", "if")


def _flat_workflow_nodes(nodes: object, found: list[dict]) -> None:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        found.append(node)
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        for nested_key in ("operations", "steps"):
            _flat_workflow_nodes(config.get(nested_key), found)


def _agent_output_reference_problem(node: dict, field: str) -> str | None:
    """Why ``field`` of this agent, llm or classifier step's result never has a value.

    A step without its own ``output_schema`` returns every field later steps
    read from it (the runner makes them its output contract), except a
    classifier, whose result is its label. A step with an ``output_schema``
    returns the reply text unless ``output_format`` is ``json``, and then only
    the fields the schema declares.
    """
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    step_type = str(node.get("type") or "").strip().lower()
    if config.get("batch") or config.get("stat_assessment_keys"):
        return None
    schema = config.get("output_schema")
    if schema is None:
        if step_type != "classifier":
            return None
        return (
            "returns its label as text. Refer to the label as '{{%s}}', or give it an output_schema "
            "that lists '%s' and output_format: 'json'" % (node["id"], field)
        )
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if schema.get("type") != "object" and not isinstance(properties, dict):
        return None
    if str(config.get("output_format") or "text").strip().lower() != "json":
        return (
            "has an output_schema but no output_format: 'json', so its result is the reply text. "
            "Set output_format: 'json' on it, or refer to the whole reply as '{{%s}}'" % node["id"]
        )
    if (
        not isinstance(properties, dict) or not properties or field in properties
        or schema.get("additionalProperties") or any(key in schema for key in _OPEN_SCHEMA_KEYWORDS)
    ):
        return None
    return (
        "has an output_schema that declares no '%s' (it declares %s). Add '%s' to its "
        "output_schema properties and required, or remove its output_schema so it returns every "
        "field later steps read from it" % (field, ", ".join(sorted(properties)), field)
    )


def _is_approval_wait(node: dict) -> bool:
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    return (
        str(node.get("type") or "").strip().lower() == "wait"
        and str(config.get("wait_type") or "approval").strip().lower() == "approval"
    )


def _approval_wait_reference_problem(node: dict) -> str:
    """An approval wait's own result is its message text; the answer is under its response_variable."""
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    variable = str(config.get("response_variable") or "").strip()
    answer = (
        f"read the decision as '{{{{{variable}.choice}}}}' and the person's note as '{{{{{variable}.note}}}}'"
        if variable else
        "give it a response_variable (e.g. 'final_approval') and read the decision as "
        "'{{final_approval.choice}}' and the person's note as '{{final_approval.note}}'"
    )
    return (
        f"results in its message text, with no fields: {answer}. No variable holds when it was decided; "
        "a workflow_project patch's history_event records that with a timestamp"
    )


def _validate_agent_output_references(
    nodes: list,
    errors: list[dict[str, str]],
    *,
    variable_names: Sequence[str] = (),
) -> None:
    """Refuse ``{{step.field}}`` reads of an agent step result that has no such field.

    A reply that stays text, or a declared output_schema without the field,
    leaves the reference without a value at run time: the step that stores
    or sends it fails, and a prompt shows the literal ``{{...}}``. A name
    another node or a definition variable also writes is ambiguous and left
    to the runner.
    """
    flat: list[dict] = []
    _flat_workflow_nodes(nodes, flat)
    writers: dict[str, int] = {}
    producers: dict[str, dict] = {}
    for node in flat:
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        shallow = {**node, "config": {key: value for key, value in config.items() if key not in {"operations", "steps"}}}
        names: set[str] = set()
        _declared_workflow_names([shallow], names)
        if isinstance(config.get("set"), dict):
            names.update(str(key).strip() for key in config["set"])
        for name in names:
            writers[name] = writers.get(name, 0) + 1
        step_type = str(node.get("type") or "").strip().lower()
        if step_type in _AGENT_OUTPUT_STEP_TYPES and isinstance(node.get("id"), str) and node["id"].strip():
            for name in (node["id"], config.get("output_var")):
                if isinstance(name, str) and name.strip():
                    producers[name.strip()] = node
        elif _is_approval_wait(node) and isinstance(node.get("id"), str) and node["id"].strip():
            producers[node["id"].strip()] = node
    declared = {str(name).strip() for name in variable_names}
    for node in flat:
        references: list[str] = []
        _template_references(node.get("config"), references)
        for reference in dict.fromkeys(references):
            match = _REFERENCE_ROOT.match(reference)
            if not match or not reference[match.end():].startswith("."):
                continue
            root = match.group(1)
            producer = producers.get(root)
            if producer is None or producer is node or writers.get(root, 0) > 1 or root in declared:
                continue
            field = reference[match.end():].lstrip(".").split(".")[0].strip()
            if _is_approval_wait(producer):
                problem = _approval_wait_reference_problem(producer)
            else:
                problem = _agent_output_reference_problem(producer, field) if field else None
            if problem:
                errors.append({
                    "code": "unknown_wait_output" if _is_approval_wait(producer) else "unknown_agent_output",
                    "message": (
                        f"Node '{node.get('id')}' refers to '{{{{{reference}}}}}', but "
                        f"{producer.get('type')} step '{producer['id']}' {problem}."
                    ),
                })


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
        node_type = str(node.get("type") or "").strip().lower()
        if node_type == "condition":
            for key in ("true_next", "false_next"):
                if key in config:
                    errors.append({
                        "code": "misplaced_condition_route",
                        "message": (
                            f"Node '{qualified_id}' config.{key} is ignored; "
                            f"move it to top-level '{key}'."
                        ),
                    })
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
            qualified_id, node_type, config, errors,
        )
        _validate_step_expectations(qualified_id, node_type, config, errors)
        _validate_step_operation(qualified_id, node_type, config, errors)
        _validate_project_folder(qualified_id, node_type, config, errors)
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
        # A trigger's production is the run's display; a workflow_project node's
        # ``production`` ("take") is a different setting of that node.
        if config.get("production") is not None and str(node_type).lower() in {"trigger", "webhook"}:
            from packages.core.services.workflow_production import normalize_workflow_production

            try:
                normalize_workflow_production(config["production"])
            except ValueError as exc:
                errors.append({
                    "code": "invalid_production",
                    "message": f"Node '{qualified_id}' has an invalid config.production: {exc}",
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
                    choices_error = record_select_config_error(schema)
                    if choices_error:
                        raise ValueError(choices_error)
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


def workflow_variable_names(variables: object) -> tuple[str, ...]:
    """Names a definition's variables declare: a ``{key: default}`` map or ``[{key}]`` list."""
    if isinstance(variables, dict):
        return tuple(str(key) for key in variables)
    if isinstance(variables, list):
        return tuple(
            str(item.get("key") or item.get("name"))
            for item in variables
            if isinstance(item, dict) and (item.get("key") or item.get("name"))
        )
    return ()


def _require_runtime_workflow(
    workflow: object,
    *,
    steps: list[dict] | None = None,
    variables: object = None,
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
        variable_names=workflow_variable_names(
            variables if variables is not None else getattr(workflow, "variables", None)
        ),
    )
    if not validation["valid"]:
        messages = "; ".join(error["message"] for error in validation["errors"])
        raise ValueError(f"Workflow is invalid: {messages}")


_INVOKE_SKILL_REFERENCE = re.compile(r'invoke_skill\(\s*skill_id\s*=\s*"([A-Za-z0-9_.:-]+)"')


def _step_skill_tool_warnings(
    step_id: str,
    step_type: str,
    config: dict,
    resolve_skill: Callable[[str], Any],
) -> list[dict[str, str]]:
    """Skills a node is written to run, whose declared tools its ``tools`` withhold.

    A node without ``tools`` resolves from its Agent's scope, as a Task or Chat
    turn of that Agent would. An explicit ``tools`` list is the whole scope of
    the node and of every Skill it invokes; a Skill then quietly runs without
    the declared tools the list leaves out, which is a defect of the node, not
    of the Skill.
    """

    tools = config.get("tools")
    if step_type not in {"agent", "llm"} or not isinstance(tools, list):
        return []
    slugs: list[str] = []
    if isinstance(config.get("skill"), str) and config["skill"].strip():
        slugs.append(config["skill"].strip())
    if "invoke_skill" in tools:
        for key in ("input", "prompt", "system_prompt"):
            if isinstance(config.get(key), str):
                slugs.extend(_INVOKE_SKILL_REFERENCE.findall(config[key]))
    if not slugs:
        return []
    from packages.core.ai.runtime.skills import runtime_skill_tools_withheld

    withheld: dict[str, tuple[str, ...]] = {}
    for slug in dict.fromkeys(slugs):
        skill = resolve_skill(slug)
        if skill is not None:
            withheld.update(runtime_skill_tools_withheld(
                skill, allowed_tool_names=tools, resolve_skill=resolve_skill,
            ))
    return [
        {
            "code": "skill_tools_withheld",
            "message": (
                f"Node '{step_id}' runs Skill '{slug}' without its declared tools "
                f"{', '.join(missing)}; add them to the node's tools or the Skill cannot use them here"
            ),
        }
        for slug, missing in withheld.items()
    ]


_NEGATED_INSTRUCTION = re.compile(r"\b(?:not|never|no|avoid|without)\b[^.;:]*$", re.IGNORECASE)
# A tool the text tells the node to use: "use analyze_audio", "call `mcp__chrome__read_page`".
_INSTRUCTED_TOOL = re.compile(
    r"\b(?:use|uses|using|call|calling|run|running|invoke|invoking|with)\s+(?:the\s+)?`?"
    r"(mcp__[a-z0-9_-]+__[a-z0-9_]+|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)`?",
    re.IGNORECASE,
)


def _step_named_tool_warnings(step_id: str, step_type: str, config: dict) -> list[dict[str, str]]:
    """Tools a node's own instructions name but its ``tools`` list leaves out.

    A node told to "check silence with analyze_audio" or to verify the post
    with ``mcp__chrome__read_page`` cannot do either when its explicit tools
    withhold them; it can only report a check it never ran, or loop on it.
    Only a registered or MCP tool name the text tells the node to use counts;
    a plan describing what a later step will call does not.
    """
    tools = config.get("tools")
    if step_type not in {"agent", "llm"} or not isinstance(tools, list):
        return []
    text = "\n".join(
        config[key] for key in ("prompt", "system_prompt", "input", "instructions")
        if isinstance(config.get(key), str)
    )
    if not text:
        return []
    from packages.core.ai.tool_pool import tool_pool

    if not tool_pool.tool_count:
        tool_pool.initialize()
    known = set(tool_pool.registered_tool_names(include_undiscoverable=True))
    named = [
        match.group(1) for match in _INSTRUCTED_TOOL.finditer(text)
        if (match.group(1) in known or match.group(1).startswith("mcp__"))
        # "Do not call list_files" withholds it on purpose.
        and not _NEGATED_INSTRUCTION.search(text[max(0, match.start() - 24):match.start()])
    ]
    allowed = {str(tool) for tool in tools}
    missing = [name for name in dict.fromkeys(named) if name not in allowed]
    if not missing:
        return []
    return [{
        "code": "instructed_tools_withheld",
        "message": (
            f"Node '{step_id}' is instructed to use {', '.join(missing)} but its tools leave "
            "them out; add them or change the instructions, or the check cannot run here"
        ),
    }]


def _first_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """The first route back to a step already on the path, as step ids, or None."""
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for target in edges.get(node, []):
            if state.get(target) == 1:
                return [*path[path.index(target):], target]
            if target in edges and target not in state:
                found = visit(target, [*path, target])
                if found:
                    return found
        state[node] = 2
        return None

    for node in edges:
        if node not in state:
            found = visit(node, [node])
            if found:
                return found
    return None


def validate_workflow_steps(
    steps: list[dict] | None,
    *,
    imported_source: str | None = None,
    resolve_skill: Callable[[str], Any] | None = None,
    variable_names: Sequence[str] = (),
) -> dict:
    """Validate a workflow graph for authoring, deployment, and execution.

    Errors make the graph unsafe to run. Warnings describe incomplete but
    saveable authoring states. This intentionally validates graph structure;
    node-specific credentials and external resources are resolved at runtime.
    ``resolve_skill`` maps a Skill slug to its declaration for the
    withheld-tool warning; packaged built-ins are resolved by default.
    ``variable_names`` are the definition's own variables, which references
    may name.
    """
    if resolve_skill is None:
        from packages.core.services.builtin_skill_loader import packaged_builtin_skill_declaration

        resolve_skill = packaged_builtin_skill_declaration
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
    if not imported_source:
        # Imported expressions are left visible on purpose (see the runner's
        # _render_template); they are mapped by the import, not authored here.
        _validate_workflow_references(nodes, errors, variable_names=variable_names)
        _validate_agent_output_references(nodes, errors, variable_names=variable_names)
    _validate_workflow_project_contracts(nodes, errors)

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
        warnings.extend(_step_skill_tool_warnings(step_id, step_type, config, resolve_skill))
        warnings.extend(_step_named_tool_warnings(step_id, step_type, config))

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

    if not imported_source:
        cycle = _first_cycle(edges_by_id)
        if cycle:
            # A step runs once its active predecessors have, so a step routed back to never runs again.
            errors.append({
                "code": "graph_cycle",
                "message": (
                    "Workflow routes back to an earlier step (" + " → ".join(cycle) + "); a run takes each step "
                    "at most once, so it would wait there forever. Repeat work with foreach_subworkflow over "
                    "the items, or let an Automation run the Workflow again"
                ),
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
        variable_names=workflow_variable_names(getattr(workflow, "variables", None)),
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


def _resolve_workflow_workspace_scope(
    workflow: WorkflowDefinition,
    requested_workspace_id: str | None,
) -> str | None:
    if workflow.workspace_id and requested_workspace_id and workflow.workspace_id != requested_workspace_id:
        raise ValueError("Workflow belongs to another workspace")
    return workflow.workspace_id or requested_workspace_id


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
    schedule_workspace_translation(wf.workspace_id)
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
    expected_version: int | None = None,
    **kwargs,
) -> WorkflowDefinition | None:
    """Apply ``kwargs`` to the workflow. With ``expected_version`` the row is
    locked and the save is refused (``StaleRevisionError``) when anyone saved
    it since that version, so two editors never silently overwrite each other."""
    if expected_version is None:
        wf = await get_workflow(db, workflow_id, entity_id)
    else:
        wf = (await db.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.id == workflow_id, WorkflowDefinition.entity_id == entity_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
    if not wf:
        return None
    if expected_version is not None and int(wf.version or 1) != int(expected_version):
        from packages.core.revisions import StaleRevisionError

        raise StaleRevisionError(
            target_kind="workflow_definition", target_id=wf.id,
            expected=int(expected_version), actual=int(wf.version or 1),
        )
    imported_source = _imported_workflow_source(wf)
    if require_valid_steps and kwargs.get("steps") is not None:
        _require_runtime_workflow(
            wf,
            steps=kwargs["steps"],
            variables=kwargs.get("variables"),
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
    schedule_workspace_translation(wf.workspace_id)
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

    Shared definitions may use an entity/automation binding when no Workspace
    is supplied. Workspace-owned definitions always retain their owning scope.

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
    workflow = await get_workflow(db, workflow_id, entity_id)
    if not workflow:
        raise ValueError("Workflow not found")
    effective_workspace_id = _resolve_workflow_workspace_scope(workflow, workspace_id)
    await _lock_workspace_workflow_configuration_scope(
        db,
        workspace_id=effective_workspace_id,
        entity_id=entity_id,
    )
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity_id,
        workflow_id=workflow_id,
        workspace_id=effective_workspace_id,
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
    schedule_workspace_translation(binding.workspace_id)
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
    workflow_id = str(changes.get("workflow_id") or binding.workflow_id)
    workflow = await get_workflow(db, workflow_id, entity_id)
    if not workflow:
        raise ValueError("Workflow not found")
    requested_workspace_id = changes.get("workspace_id", binding.workspace_id)
    effective_workspace_id = _resolve_workflow_workspace_scope(workflow, requested_workspace_id)
    await _lock_workspace_workflow_configuration_scope(
        db,
        workspace_id=effective_workspace_id,
        entity_id=entity_id,
    )
    changes["workspace_id"] = effective_workspace_id
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
    schedule_workspace_translation(binding.workspace_id)
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


SCHEDULED_WORKFLOW_RESERVED_INPUT_KEYS = frozenset({"payload_message"})


async def schedule_workflow(
    db: AsyncSession,
    entity_id: str,
    workflow_id: str,
    *,
    cron: str | None = None,
    run_at: datetime | str | None = None,
    name: str | None = None,
    workspace_id: str | None = None,
    timezone_str: str = "UTC",
    created_by: str | None = None,
    conversation_id: str | None = None,
    inputs: dict | None = None,
    stop_after: datetime | str | None = None,
    idempotency_key: str | None = None,
    owner_project_id: str | None = None,
):
    """Schedule a workflow on a cron, or once at ``run_at`` — as an **automation**.

    Scheduling is an automation concern, not a workflow binding: this creates a
    ScheduledJob (``execution_type="workflow"``) that lives in the Automations
    system and references the workflow via ``execution_target.workflow_id``. The
    existing Celery-Beat ``scheduler.tick`` then fires it, creating a WorkflowRun
    via the runner's ``exec_type="workflow"`` dispatch. The workflow itself stays
    independent — this is just the timer that calls it. A one-shot ``run_at``
    already in the past fires on the next tick.
    """
    if bool(cron) == (run_at is not None):
        raise ValueError("Scheduled workflow requires exactly one of cron or run_at")
    if run_at is not None:
        if isinstance(run_at, str):
            try:
                run_at = datetime.fromisoformat(run_at)
            except ValueError as exc:
                raise ValueError("run_at must be an ISO timestamp") from exc
        if not isinstance(run_at, datetime) or run_at.tzinfo is None:
            raise ValueError("run_at must be a timezone-aware timestamp")
        # One UTC spelling keeps idempotent replays of the same instant equal.
        run_at = run_at.astimezone(timezone.utc).isoformat()
    from packages.core.services.scheduler_service import create_scheduled_job
    from packages.core.services.workspace_workflow_router import (
        required_workflow_run_inputs_without_default,
        validate_workflow_run_input_values,
        workflow_run_inputs,
    )

    wf = await get_workflow(db, workflow_id, entity_id)
    if not wf:
        raise ValueError("Workflow not found")
    if wf.workspace_id and wf.workspace_id != workspace_id:
        raise ValueError("Scheduled workflow belongs to another Workspace")
    from packages.core.models.task import Conversation

    if not conversation_id and workspace_id:
        conversation_id = await db.scalar(
            select(Conversation.id).where(
                Conversation.entity_id == entity_id,
                Conversation.workspace_id == workspace_id,
                Conversation.scope == "workspace_main",
                Conversation.status == "active",
            ).limit(1)
        )
    if conversation_id:
        conversation = await db.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.entity_id != entity_id
            or conversation.workspace_id != workspace_id
            or conversation.status != "active"
            or (conversation.scope == "channel" and conversation.user_id != created_by)
        ):
            raise ValueError("Scheduled conversation is unavailable in this Workspace")
    inputs = dict(inputs or {})
    declared_input_keys = {str(item["key"]) for item in workflow_run_inputs(wf)}
    reserved_input_keys = set(inputs) & SCHEDULED_WORKFLOW_RESERVED_INPUT_KEYS
    if reserved_input_keys:
        raise ValueError(
            "Scheduled workflow inputs are reserved for scheduler runtime: "
            + ", ".join(sorted(reserved_input_keys))
        )
    unknown_input_keys = set(inputs) - declared_input_keys
    if unknown_input_keys:
        raise ValueError("Scheduled workflow inputs are undeclared: " + ", ".join(sorted(unknown_input_keys)))
    missing = [key for key in required_workflow_run_inputs_without_default(wf) if key not in inputs]
    if missing:
        raise ScheduledWorkflowInputsError(missing)
    inputs = validate_workflow_run_input_values(workflow_run_inputs(wf), inputs)

    job = await create_scheduled_job(
        db, entity_id,
        job_id=f"wf-{generate_ulid()}",
        name=name or "Scheduled workflow",
        schedule_kind="at" if run_at is not None else "cron",
        cron_expr=str(cron) if cron else None,
        run_at=run_at,
        timezone_str=timezone_str,
        execution_type="workflow",
        execution_target={
            "workflow_id": workflow_id,
            **({"inputs": inputs} if inputs else {}),
            **({"workspace_id": workspace_id} if workspace_id else {}),
        },
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=created_by,
        require_actor_workspace_write=bool(created_by),
        stop_after=stop_after,
        idempotency_key=idempotency_key,
        owner_project_id=owner_project_id,
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
        str(step.get("id")): workflow_step_label(step)
        for step in (workflow.steps or [])
        if isinstance(step, dict) and str(step.get("id") or "").strip()
    }
    metadata = {
        "_workflow_name": str(workflow.name or workflow.id),
        "_workflow_node_names": node_names,
    }
    translations = run_display_translations(workflow.steps)
    if translations:
        metadata[RUN_DISPLAY_I18N_KEY] = translations
    return metadata


async def _name_run_after_binding(db: AsyncSession, trigger_data: dict, binding_id: str | None) -> None:
    """A run started from a Workspace's Flow is shown by that Flow's name.

    Built-in definitions keep their stable slug as the technical name; the
    binding carries what the person sees in the App and Flows, and its
    translations (``config.i18n.<locale>.name``).
    """
    if not binding_id:
        return
    row = (await db.execute(
        select(WorkflowBinding.name, WorkflowBinding.config).where(WorkflowBinding.id == binding_id)
    )).one_or_none()
    name, config = row if row is not None else (None, None)
    if isinstance(name, str) and name.strip():
        trigger_data["_workflow_name"] = name.strip()
        named = run_display_translations(
            [], name=name, name_translations=binding_name_translations(name, config),
            texts=chat_entrypoint_title_translations(config),
        )
        translations = trigger_data.get(RUN_DISPLAY_I18N_KEY)
        translations = deepcopy(translations) if isinstance(translations, dict) else {}
        for code in set(translations) | set(named):
            # The step names come from the Flow; its name comes from the binding.
            entry = {key: value for key, value in (translations.get(code) or {}).items() if key != "name"}
            translations[code] = {**entry, **named.get(code, {})}
        translations = {code: entry for code, entry in translations.items() if entry}
        if translations:
            trigger_data[RUN_DISPLAY_I18N_KEY] = translations
        else:
            trigger_data.pop(RUN_DISPLAY_I18N_KEY, None)


def _snapshot_display_metadata(snapshot: dict) -> dict:
    node_names = {
        str(node.get("id")): workflow_step_label(node)
        for node in (snapshot.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }
    metadata = {
        "_workflow_name": str(
            snapshot.get("name") or snapshot.get("workflow_id") or ""
        ),
        "_workflow_node_names": node_names,
    }
    translations = run_display_translations(snapshot.get("nodes"))
    if translations:
        metadata[RUN_DISPLAY_I18N_KEY] = translations
    return metadata

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


def _retry_structured_wait_input(step: dict, result: dict) -> tuple[str, dict] | None:
    """Recover a schema-validated wait response instead of its display text."""
    if step.get("type") != "wait":
        return None
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    schema = config.get("input_schema")
    if (
        config.get("wait_type") != "input"
        or config.get("collection_fields")
        or not isinstance(schema, dict)
        or schema.get("type") != "object"
    ):
        return None
    response = result.get("workflow_response")
    payload = response.get("payload") if isinstance(response, dict) else None
    if not isinstance(payload, dict):
        return None
    response_variable = str(config.get("response_variable") or "inputs").strip() or "inputs"
    value = payload.get(response_variable)
    if not isinstance(value, dict):
        value = payload.get("inputs")
    properties = schema.get("properties")
    if not isinstance(value, dict) or not isinstance(properties, dict):
        return None
    value = {key: value[key] for key in properties if key in value}
    if list(SchemaContractValidatorFactory.build(schema).iter_errors(value)):
        return None
    return response_variable, value


def _retry_variables(
    workflow: WorkflowDefinition,
    prior: WorkflowRun,
    inherited_step_ids: set[str],
    variables: dict | None,
    retry_from_step_id: str | None = None,
    *,
    retry_step_ids: set[str] | None = None,
    source_steps: list[dict] | None = None,
) -> dict:
    """``source_steps`` is the definition the prior attempt ran, when it differs."""
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

    # Rehydrate completed outputs in their execution dependency order. Blueprint
    # arrays are not an execution log: a recovery node can be declared after a
    # branch it conditionally precedes. Applying inherited outputs in definition
    # order would let that earlier branch overwrite a later producer sharing an
    # output variable.
    inherited_order_index = {
        str(step.get("id") or ""): index
        for index, step in enumerate(workflow.steps or [])
    }
    inherited_order_ids = {
        step_id for step_id in inherited_step_ids if step_id in inherited_order_index
    }
    inherited_successors: dict[str, set[str]] = {
        step_id: set() for step_id in inherited_order_ids
    }
    inherited_indegree = {step_id: 0 for step_id in inherited_order_ids}

    def add_inherited_edge(before: str, after: str) -> None:
        if before not in inherited_order_ids or after not in inherited_order_ids:
            return
        if after not in inherited_successors[before]:
            inherited_successors[before].add(after)
            inherited_indegree[after] += 1

    for step in workflow.steps or []:
        step_id = str(step.get("id") or "")
        if step_id not in inherited_order_ids:
            continue
        for target in _selected_step_targets(step, prior_results.get(step_id)):
            add_inherited_edge(step_id, target)
        # ``depends_on`` points from the consumer to its producer.
        for dependency in step.get("depends_on") or []:
            add_inherited_edge(str(dependency), step_id)
    inherited_ready = sorted(
        (step_id for step_id, degree in inherited_indegree.items() if degree == 0),
        key=inherited_order_index.__getitem__,
    )
    inherited_execution_order: list[str] = []
    while inherited_ready:
        step_id = inherited_ready.pop(0)
        inherited_execution_order.append(step_id)
        for successor in sorted(inherited_successors[step_id], key=inherited_order_index.__getitem__):
            inherited_indegree[successor] -= 1
            if inherited_indegree[successor] == 0:
                inherited_ready.append(successor)
        inherited_ready.sort(key=inherited_order_index.__getitem__)
    # Cyclic/legacy malformed graphs retain their stable definition ordering.
    inherited_execution_order.extend(sorted(
        inherited_order_ids - set(inherited_execution_order),
        key=inherited_order_index.__getitem__,
    ))
    stage_execution = (
        updated.get("__stage_execution")
        if isinstance(updated.get("__stage_execution"), dict)
        else {}
    )
    retry_stage_ids = retry_step_ids if retry_step_ids is not None else {str(retry_from_step_id or "").strip()}
    retained_stage_states = {
        sid: state for sid, state in stage_execution.items()
        if sid in retry_stage_ids and isinstance(state, dict)
        and ((prior_results.get(sid) or {}).get("status") != "completed"
             or (prior_results.get(sid) or {}).get("needs_person"))
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

        # Collection waits retain their submission envelope separately from the
        # display receipt.  Replaying one must ask for a new answer instead of
        # consuming its former response from the retry variable bag.
        collection_fields = config.get("collection_fields")
        if (
            step.get("type") == "wait"
            and config.get("wait_type") in {"input", "human_input"}
            and isinstance(collection_fields, list)
            and collection_fields
        ):
            response_variable = str(
                config.get("response_variable") or f"{step_id}_response"
            ).strip()
            if response_variable and response_variable not in declared_input_roots:
                updated.pop(response_variable, None)

            collection_values = operation_result.get("collection_values")
            if not isinstance(collection_values, dict):
                return
            for field in collection_fields:
                if not isinstance(field, dict):
                    continue
                key = str(field.get("key") or "").strip()
                if (
                    key
                    and key not in declared_input_roots
                    and prior_variables.get(key) == collection_values.get(key)
                ):
                    updated.pop(key, None)
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

    # A node edited or removed since the prior attempt wrote its outputs under
    # its old names; clear those too so none leaks into the current graph.
    for step in [*(source_steps or []), *(workflow.steps or [])]:
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
    steps_by_id = {
        str(step.get("id") or ""): step for step in workflow.steps or []
    }
    for step_id in inherited_execution_order:
        step = steps_by_id[step_id]
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        output_var = str(config.get("output_var") or "").strip()
        result = prior_results.get(step_id) or {}
        if (result.get("status") == "completed" or result.get("continued") is True) and "output" in result:
            structured_wait_input = _retry_structured_wait_input(step, result)
            step_output = deepcopy(result["output"])
            updated[step_id] = step_output
            if output_var:
                updated[output_var] = deepcopy(step_output)
            if structured_wait_input is not None:
                response_variable, input_value = structured_wait_input
                updated[response_variable] = deepcopy(input_value)
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


async def _retry_production_progress(
    db: AsyncSession, prior: WorkflowRun, steps: list[dict], step_id: str,
) -> dict | None:
    """The units every attempt of this result already made, for a retried step that makes units.

    A retry redoes only what came up short: the clips earlier attempts
    finished are handed to the step, and the units still to make are named.
    """
    from packages.core.models.media_job import MediaJob
    from packages.core.services.workflow_production import (
        production_progress, production_unit_steps, workflow_production_config, workflow_run_local_clips,
    )

    config = workflow_production_config(steps)
    if config is None or step_id not in production_unit_steps(config):
        return None
    root = prior.lineage_root_run_id or prior.id
    run_ids = list((await db.scalars(select(WorkflowRun.id).where(
        WorkflowRun.entity_id == prior.entity_id,
        WorkflowRun.workflow_id == prior.workflow_id,
        or_(WorkflowRun.id == root, WorkflowRun.lineage_root_run_id == root),
    ).limit(RUN_FAMILY_MAX_RUNS))).all())
    jobs = (await db.scalars(select(MediaJob).where(
        MediaJob.entity_id == prior.entity_id,
        MediaJob.params["run_id"].astext.in_([*run_ids, prior.id]),
    ).limit(500))).all()
    return production_progress(config, prior.variables or {}, [{
        "id": job.id, "status": job.status, "kind": job.kind,
        "unit_id": (job.params or {}).get("unit_id"),
        "output_name": (job.params or {}).get("output_name"),
        "document_id": (job.params or {}).get("result_document_id"),
        "result_url": job.result_url, "duration_seconds": job.duration_seconds,
        "created_at": job.created_at.isoformat() if job.created_at else "",
    } for job in jobs] + await workflow_run_local_clips(
        db, entity_id=prior.entity_id, run_ids=[*run_ids, prior.id],
    ))


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
    from packages.core.services.workflow_run_trace import workflow_result_outcome

    outcome = str(state.get("business_outcome") or workflow_result_outcome(prior.variables)).strip() or None
    retry_state = state.get("retry_state")
    if not isinstance(retry_state, dict):
        # Flows without a project record their repair hint on the step they stopped at.
        current_result = (prior.step_results or {}).get(str(prior.current_step_id or ""))
        retry_state = current_result.get("retry_state") if isinstance(current_result, dict) else None
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
        workflow_run_input_rules,
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
        input_rules=workflow_run_input_rules(workflow),
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
    trusted_runtime_context: dict | None = None,
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
    effective_workspace_id = _resolve_workflow_workspace_scope(wf, workspace_id)
    await _assert_workspace_workflow_execution_ready(
        db,
        workspace_id=effective_workspace_id,
        entity_id=entity_id,
        trigger_source=trigger_source,
        started_by=started_by,
        trigger_data=trigger_data,
        workflow_id=workflow_id,
    )
    await _validate_workspace_service_dependencies(
        db, workflow=wf, workspace_id=effective_workspace_id,
    )
    from packages.core.services.workspace_workflow_router import (
        check_declared_workflow_record_selections,
        check_declared_workflow_run_inputs,
    )

    if trigger_source in DIRECT_RUN_INPUT_CHECKED_SOURCES:
        # The Run dialog enforces these client-side; API, MCP and scheduled
        # starts must not reach paid nodes with a required input missing.
        check_declared_workflow_run_inputs(
            wf, variables=variables, trigger_data=trigger_data,
        )
    # Picked records must exist in their source however the run starts.
    await check_declared_workflow_record_selections(
        db, wf, variables=variables, trigger_data=trigger_data,
        entity_id=entity_id, workspace_id=effective_workspace_id,
    )

    # Merge workflow-level default variables with runtime overrides
    merged_vars = dict(wf.variables or {})
    if variables:
        merged_vars.update(variables)
    from packages.core.services.workspace_workflow_router import (
        apply_declared_run_input_defaults,
        apply_declared_run_input_values,
    )

    merged_vars = apply_declared_run_input_values(
        wf, merged_vars, trigger_data, explicit_keys=(variables or {}).keys(),
    )
    merged_vars = apply_declared_run_input_defaults(wf, merged_vars, trigger_data)

    steps = wf.steps or []
    first_step_id = require_entry_step_id(steps)
    _require_runtime_workflow(wf)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)
    await _name_run_after_binding(db, attempt_trigger_data, binding_id)
    if trusted_runtime_context:
        attempt_trigger_data["_workflow_runtime_context"] = deepcopy(trusted_runtime_context)
    if started_by and trigger_source in MANUAL_WORKFLOW_TRIGGER_SOURCES:
        attempt_trigger_data["_workspace_user_request"] = {"user_id": started_by}

    run_id = generate_ulid()
    run = WorkflowRun(
        id=run_id,
        workflow_id=workflow_id,
        entity_id=entity_id,
        workspace_id=effective_workspace_id,
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
    trusted_runtime_context: dict | None = None,
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
    if binding.workspace_id and execution_workspace_id and binding.workspace_id != execution_workspace_id:
        raise ValueError("Workflow binding belongs to another workspace")
    effective_workspace_id = _resolve_workflow_workspace_scope(
        wf, binding.workspace_id or execution_workspace_id,
    )
    await _assert_workspace_workflow_execution_ready(
        db,
        workspace_id=effective_workspace_id,
        entity_id=binding.entity_id,
        trigger_source=trigger_source or binding.trigger_type,
        user_requested=user_requested,
        started_by=started_by,
        trigger_data=trigger_data,
        workflow_id=wf.id,
        binding=binding,
    )
    await _validate_workspace_service_dependencies(
        db, workflow=wf, workspace_id=effective_workspace_id,
    )
    from packages.core.services.workspace_workflow_router import (
        check_declared_workflow_record_selections,
    )

    # Picked records must exist in their source however the run starts.
    await check_declared_workflow_record_selections(
        db, wf, variables={**(binding.variables or {}), **(variables or {})},
        trigger_data=trigger_data, entity_id=binding.entity_id,
        workspace_id=effective_workspace_id,
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
    from packages.core.services.workspace_workflow_router import (
        apply_declared_run_input_defaults,
    )

    merged_vars = apply_declared_run_input_defaults(wf, merged_vars, trigger_data)

    steps = wf.steps or []
    first_step_id = require_entry_step_id(steps)
    _require_runtime_workflow(wf)
    attempt_trigger_data = _attempt_trigger_data(wf, trigger_data)
    await _name_run_after_binding(db, attempt_trigger_data, binding.id)
    if trusted_runtime_context:
        attempt_trigger_data["_workflow_runtime_context"] = deepcopy(trusted_runtime_context)
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


async def _stale_inherited_workflow_project(
    db: AsyncSession,
    prior: WorkflowRun,
    steps: dict[str, dict],
    inherited_results: dict[str, Any],
    rerun_step_ids: Sequence[str],
) -> str | None:
    """Name the Workflow project a retry would write back over a newer version.

    A retry reuses the project a kept step read, and the steps it runs again
    patch that project against the revision read then. When a later run changed
    the project meanwhile, those writes are refused at the end, after all of the
    retry's work ran. A project the retry reads again is current.
    """
    rerun = [steps.get(step_id) or {} for step_id in rerun_step_ids]
    rerun = [step for step in rerun if step.get("type") == "workflow_project"]
    reread = {
        str((step.get("config") or {}).get("output_var") or "")
        for step in rerun if (step.get("config") or {}).get("operation") != "patch"
    }
    patched = {
        match.group(1)
        for step in rerun if (step.get("config") or {}).get("operation") == "patch"
        for match in [re.fullmatch(r"\{\{\s*(\w+)\.project_id\s*\}\}", str((step.get("config") or {}).get("project_id") or "").strip())]
        if match
    }
    kept: dict[str, str] = {}
    for step_id, result in inherited_results.items():
        output = result.get("output") if isinstance(result, dict) else None
        output_var = str(result.get("output_var") or "") if isinstance(result, dict) else ""
        if (
            (steps.get(step_id) or {}).get("type") == "workflow_project"
            and isinstance(output, dict) and output.get("project_id")
            and output_var in patched and output_var not in reread
        ):
            kept[str(output["project_id"])] = str((output.get("state") or {}).get("title") or output.get("project_key") or "")
    return await _workflow_project_replaced(db, prior, kept)


async def workflow_run_superseded_project(db: AsyncSession, run: WorkflowRun) -> str | None:
    """Name the Workflow project a newer run has made since this run stopped.

    Going on with this run (a retry or recovery from where it stopped) would
    write that project back over the newer version, so it is no longer offered."""
    if run.status not in {"failed", "cancelled"}:
        return None
    definition = await db.scalar(select(WorkflowDefinition.steps).where(
        WorkflowDefinition.id == run.workflow_id, WorkflowDefinition.entity_id == run.entity_id,
    ))
    steps = {str(step.get("id") or ""): step for step in definition or [] if isinstance(step, dict)}
    done = {
        step_id: result for step_id, result in (run.step_results or {}).items()
        if isinstance(result, dict) and result.get("status") == "completed"
    }
    # Going on runs the step it stopped at and what follows it.
    ahead: list[str] = []
    pending = [str(run.current_step_id or "")]
    while pending:
        step_id = pending.pop()
        if step_id in steps and step_id not in ahead:
            ahead.append(step_id)
            step = steps[step_id]
            pending.extend(str(item) for key in ("next", "true_next", "false_next") for item in step.get(key) or [])
    return await _stale_inherited_workflow_project(
        db, run, steps, {step_id: result for step_id, result in done.items() if step_id not in ahead}, ahead,
    )


async def workflow_project_replaced_by_other_run(db: AsyncSession, run: WorkflowRun, project_id: str) -> bool:
    """Whether a run outside this run's family wrote the project last."""
    last_run_id = await db.scalar(select(WorkflowProject.last_run_id).where(
        WorkflowProject.entity_id == run.entity_id, WorkflowProject.id == project_id,
    ))
    writer = await db.get(WorkflowRun, last_run_id) if last_run_id else None
    return writer is not None and (writer.lineage_root_run_id or writer.id) != (run.lineage_root_run_id or run.id)


async def _workflow_project_replaced(db: AsyncSession, run: WorkflowRun, kept: dict[str, str]) -> str | None:
    if not kept:
        return None
    # What this run last saw of each project, its own writes included.
    seen: dict[str, int] = {}
    for result in (run.step_results or {}).values():
        output = result.get("output") if isinstance(result, dict) else None
        if isinstance(output, dict) and str(output.get("project_id") or "") in kept:
            pid = str(output["project_id"])
            seen[pid] = max(seen.get(pid, 0), int(output.get("revision") or 0))
    current = await db.execute(select(WorkflowProject.id, WorkflowProject.revision, WorkflowProject.last_run_id).where(
        WorkflowProject.entity_id == run.entity_id, WorkflowProject.id.in_(list(kept)),
    ))
    for project_id, revision, last_run_id in current.all():
        if int(revision or 0) <= seen.get(project_id, 0) or not last_run_id:
            continue
        # Only another run's newer version; a change made for this run's own
        # family (a lease handed over, a verification) is settled when it runs.
        if await workflow_project_replaced_by_other_run(db, run, project_id):
            return f"project {kept[project_id]!r}" if kept[project_id] else "its project"
    return None


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


async def stalled_run_restart(db: AsyncSession, run: WorkflowRun) -> tuple[WorkflowBinding, dict] | None:
    """The binding and original inputs to start a stalled run again on its current Flow.

    A run whose frozen Flow could not advance stalls again on every retry of that
    Flow. Once the Flow has changed, Retry means starting over on the current
    version with the same inputs; while it has not, there is nothing to retry.
    """
    from packages.core.services.workflow_run_trace import workflow_run_restarted_as, workflow_run_stalled

    if not workflow_run_stalled(run) or workflow_run_restarted_as(run) or not run.binding_id:
        return None
    binding = await get_binding(db, run.binding_id, run.entity_id)
    workflow = await get_workflow(db, run.workflow_id, run.entity_id)
    if binding is None or workflow is None:
        return None
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    if str(snapshot.get("fingerprint") or "") == workflow_definition_fingerprint(workflow):
        return None
    # The inputs the person gave, as the current Flow declares them; internal keys stay behind.
    # The trigger keeps them as given; a step may since have rewritten the variable.
    trigger = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    variables = run.variables if isinstance(run.variables, dict) else {}
    start = next((step for step in workflow.steps or [] if isinstance(step, dict) and step.get("type") == "trigger"), {})
    declared = [str(item.get("key") or "") for item in (start.get("config") or {}).get("run_inputs") or []
                if isinstance(item, dict)]
    inputs = {key: deepcopy(trigger[key] if key in trigger else variables[key])
              for key in declared if key and (key in trigger or key in variables)}
    return binding, inputs


async def restart_stalled_run(
    db: AsyncSession, stalled: WorkflowRun, *, user: Any, variables: dict | None = None,
) -> tuple[WorkflowRun, Any]:
    """Retry a run its frozen Flow could not advance: start it again on the current Flow.

    Every Retry surface comes here. The original inputs (with any the person
    changed) go through the same launch as a manual run, under the same App
    entry; the stalled run records the new one, so a second Retry starts
    nothing. Uncommitted: the caller commits, then ``enqueue_binding_run``.
    """
    from packages.core.services.workflow_chat_projection import project_workflow_run_status
    from packages.core.services.workflow_run_trace import (
        STALLED_RESTARTED_KEY, STALLED_RETRY_ERROR, workflow_run_restarted_as,
    )
    from packages.core.services.workspace_flow_launcher import start_binding_run

    locked = await db.scalar(
        select(WorkflowRun).where(WorkflowRun.id == stalled.id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise ValueError("Workflow run not found")
    if workflow_run_restarted_as(locked):
        raise ValueError("Workflow retry is blocked: this run was already started again on the Flow's current version.")
    restart = await stalled_run_restart(db, locked)
    if restart is None:
        raise ValueError(f"Workflow retry is blocked: {STALLED_RETRY_ERROR}")
    binding, inputs = restart
    inputs.update(variables or {})
    previous = dict(locked.trigger_data or {})
    trigger = {"workspace_manual": True}
    if previous.get(WORKSPACE_APP_ACTION_KEY):
        trigger[WORKSPACE_APP_ACTION_KEY] = previous[WORKSPACE_APP_ACTION_KEY]
    run, launched = await start_binding_run(
        db, binding=binding, user=user, variables=inputs, trigger_data=trigger,
        workspace_scoped=bool(binding.workspace_id),
    )
    locked.trigger_data = {**previous, STALLED_RESTARTED_KEY: run.id}
    await project_workflow_run_status(db, run=locked)
    return run, launched


async def _supplied_step_output_audit(
    db: AsyncSession,
    prior: WorkflowRun,
    *,
    steps: dict[str, dict],
    supplied: dict,
    requested_by: str,
) -> dict:
    """Check work finished outside a run before it stands in for one step.

    The output must satisfy the step's own declared contract and name each
    supplied Knowledge Document. Each Document must be readable, in the run's
    Workspace, by the person supplying it and by the person the run continues
    as. Nothing is skipped: the run continues from that step, so its checks and
    reviews still run.
    """
    from packages.core.models.document import Document
    from packages.core.services.document_access import user_can_read_document
    from packages.core.services.workflow_agent_recovery import step_output_contract
    from packages.core.services.workflow_retry import _DETERMINISTIC_STEP_TYPES

    step_id = str(supplied.get("step_id") or "").strip()
    step = steps.get(step_id)
    if step is None:
        raise ValueError("Supplied output names a step that is not part of the workflow")
    if step.get("type") in _DETERMINISTIC_STEP_TYPES or step.get("type") == "wait":
        raise ValueError("Supplied output can only stand in for a step that produces work")
    schema = step_output_contract(step)
    if schema is None:
        raise ValueError(f"Supplied output needs step '{step_id}' to declare an output_schema")
    output = supplied.get("output")
    if not isinstance(output, dict):
        raise ValueError("Supplied output must be an object")
    errors = sorted(
        SchemaContractValidatorFactory.build(schema).iter_errors(output),
        key=lambda error: list(error.path),
    )
    if errors:
        where = ".".join(str(part) for part in errors[0].path) or "output"
        raise ValueError(f"Supplied output does not match step '{step_id}' output_schema at {where}: {errors[0].message}")
    document_ids = [str(item or "").strip() for item in supplied.get("document_ids") or []]
    if not document_ids or not all(document_ids) or len(document_ids) > 20 or len(set(document_ids)) != len(document_ids):
        raise ValueError("Supplied output needs 1-20 distinct document_ids")

    def strings(value: object) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return set().union(*(strings(child) for child in value.values()))
        if isinstance(value, list):
            return set().union(*(strings(child) for child in value))
        return set()

    named = strings(output)
    if not requested_by:
        raise ValueError("Supplied output needs the person who supplies it")
    readers = [await db.get(User, user_id) for user_id in dict.fromkeys([requested_by, prior.started_by]) if user_id]
    for document_id in document_ids:
        if document_id not in named:
            raise ValueError(f"Supplied output does not reference document '{document_id}'")
        document = await db.get(Document, document_id)
        if document is None or document.is_trashed or not all([
            reader is not None and await user_can_read_document(
                db, document, entity_id=prior.entity_id, user_id=reader.id,
                role=reader.role, workspace_id=prior.workspace_id,
            )
            for reader in readers
        ]):
            raise ValueError(f"Supplied document '{document_id}' isn't a readable Document in this run's Workspace")
    note = str(supplied.get("note") or "").strip()
    if len(note) > 2000:
        raise ValueError("Supplied output note must be at most 2000 characters")
    return {
        "step_id": step_id,
        "document_ids": document_ids,
        "note": note,
        "source_run_id": prior.id,
        "supplied_by": requested_by,
        "supplied_at": datetime.now(timezone.utc).isoformat(),
    }


# Tools that only read, render, merge, or save files inside the Workspace.  A
# provider outage during them left nothing outside Manor, so a retry repeats
# billed generation but cannot repeat a publish or a Chrome action.
GENERATION_ONLY_TOOLS = frozenset({
    "read_file", "generate_file", "generate_image", "generate_video", "still_to_video",
    "render_frame_samples", "merge_videos", "probe_media", "wait_media_jobs", "workspace_search",
})


def provider_outage_without_external_effect(result: Any) -> bool:
    """True when a failed attempt stopped in a provider outage and used only generation tools."""
    if not isinstance(result, dict) or result.get("provider_transient") is not True:
        return False
    tools = result.get("tools_used")
    return isinstance(tools, list) and bool(tools) and all(tool in GENERATION_ONLY_TOOLS for tool in tools)


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
    effect_review: dict | None = None,
    definition: str = "original",
    revision: bool = False,
    supplied_output: dict | None = None,
) -> WorkflowRun:
    """Create a linked attempt that resumes from a prior run checkpoint.

    ``supplied_output`` (``step_id``, ``output``, ``document_ids``, ``note``)
    accepts work finished outside the run, such as a file made in chat, as
    that step's result: the step is not run again and everything after it,
    including its checks and reviews, runs as usual (see
    ``_supplied_step_output_audit``).

    ``revision`` makes a finished run's next version: the person asked for a
    change, so a completed run may start again from the step its Workflow's
    ``production.revise`` names (callers check that declaration).

    ``expected_latest_run_id`` lets internal continuations atomically refuse to
    replace a newer family attempt or revive one explicitly cancelled after a
    crash. Interactive retries retain their existing cancelled-attempt policy.

    ``definition="current"`` runs the attempt on the Workflow as it is now
    (a person fixed it after the failure) while still resuming at the
    checkpoint: completed receipts of nodes that still exist are reused even
    when edited, removed nodes' receipts are dropped, and a node added on the
    taken path before the checkpoint runs first. Effect gates read the
    definition the prior attempt actually ran.

    ``started_by`` is the person asking for the retry. When that is not the
    person who started the run (a manager or a provider taking over a
    customer's run), the attempt continues as the run's starter: their
    accounts, recipients and approvals stay theirs, and it runs the frozen
    definition they started, never one edited since. The person who asked is
    recorded under ``RETRY_REQUESTED_BY_KEY``.
    """
    if definition not in {"original", "current"}:
        raise ValueError("Workflow retry definition must be 'original' or 'current'")
    prior = await get_run(db, run_id, entity_id)
    if prior is None:
        raise ValueError("Workflow run not found")
    retried_for_starter = bool(
        expected_latest_run_id is None
        and started_by
        and prior.started_by
        and str(prior.started_by) != str(started_by)
    )
    acting_user_id = prior.started_by if retried_for_starter else started_by
    if retried_for_starter:
        definition = "original"
        if prior.workspace_id:
            from packages.core.services.workspace_access import user_can_read_workspace_id

            if not await user_can_read_workspace_id(
                db,
                workspace_id=prior.workspace_id,
                entity_id=entity_id,
                user_id=acting_user_id,
            ):
                raise ValueError(
                    "The person who started this run can no longer access its "
                    "Workspace; start a new run instead"
                )
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
            started_by=acting_user_id,
            trigger_data=prior.trigger_data,
            workflow_id=prior.workflow_id,
            existing_configured_origin=(
                expected_latest_run_id is not None
                and prior.trigger_source == "schedule"
            ),
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
    from packages.core.services.workflow_run_trace import STALLED_RETRY_ERROR, workflow_run_stalled

    if workflow_run_stalled(prior):
        raise ValueError(f"Workflow retry is blocked: {STALLED_RETRY_ERROR}")
    business_outcome, retry_state = await _retry_business_state(db, prior)
    retryable_business_outcome = (
        prior.status == "completed"
        and business_outcome in {"needs_input", "revision_required"}
    )
    if prior.status not in {"failed", "cancelled"} and not retryable_business_outcome and not (
        revision and prior.status == "completed"
    ):
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
    source_fingerprint = (
        str(prior_execution_snapshot["fingerprint"])
        if prior_execution_snapshot is not None
        else stored_fingerprint
    )
    use_current = definition == "current" and source_fingerprint != current_fingerprint
    if prior_execution_snapshot is None:
        if not use_current and (not stored_fingerprint or stored_fingerprint != current_fingerprint):
            raise ValueError(
                "Workflow definition changed since the failed attempt; this legacy "
                "run has no immutable execution snapshot"
            )
        # A legacy attempt kept no copy of the graph it ran.
        source_workflow = workflow
        prior_execution_snapshot = build_execution_snapshot(
            workflow,
            fingerprint=current_fingerprint,
        )
    else:
        source_workflow = workflow_execution_view(workflow, prior)
    # The prior receipts and their effect gates belong to ``source_workflow``;
    # the new attempt plans and runs on ``execution_workflow``.
    execution_workflow = workflow if use_current else source_workflow

    source_steps = {
        str(step.get("id") or ""): step
        for step in source_workflow.steps or []
        if isinstance(step, dict)
    }
    workflow_steps = {
        str(step.get("id") or ""): step
        for step in execution_workflow.steps or []
        if isinstance(step, dict)
    }

    supplied_audit = supplied_result = None
    if supplied_output is not None:
        if effect_review is not None or revision or variables or expected_latest_run_id is not None:
            raise ValueError("Supplied output can't be combined with an effect review, revision or changed inputs")
        supplied_audit = await _supplied_step_output_audit(
            db, prior, steps=workflow_steps, supplied=supplied_output, requested_by=started_by,
        )
        if supplied_audit["step_id"] not in source_steps:
            raise ValueError("Supplied output names a step the failed attempt did not have")
        if from_step_id and from_step_id != supplied_audit["step_id"]:
            raise ValueError("Supplied output must be for the retry step")
        from_step_id = supplied_audit["step_id"]
        supplied_result = deepcopy(supplied_output["output"])

    def was_supplied(step_id: str) -> bool:
        # The supplied step is not run again, so its old outcome can't replay.
        return bool(supplied_audit and step_id == supplied_audit["step_id"])

    from packages.core.services.workflow_agent_recovery import (
        workflow_cancelled_agent_requires_review,
    )
    if (
        effect_review is None
        and workflow_cancelled_agent_requires_review(prior)
        and not was_supplied(str(prior.current_step_id or ""))
    ):
        raise ValueError(
            "Workflow retry is blocked: the cancelled agent step must be reconciled first"
        )

    reviewed_result = None
    review_audit = None
    if effect_review is not None:
        from packages.core.constants.tool_effects import EffectReconciliationDecision
        from packages.core.services.workflow_agent_recovery import (
            workflow_effect_review, workflow_effect_review_blocker, workflow_effect_result_schema,
            workflow_released_project_reviews,
        )

        review = workflow_effect_review(prior)
        note = str(effect_review.get("note") or "").strip()
        applied_review = effect_review.get("decision") == EffectReconciliationDecision.APPLIED.value
        if (
            expected_latest_run_id is not None or not started_by
            or latest_family_attempt.id != prior.id
            or not review
            or effect_review.get("step_id") != review["step_id"]
            or effect_review.get("effect_fingerprint") != review["effect_fingerprint"]
            or effect_review.get("decision") not in {
                EffectReconciliationDecision.NOT_APPLIED.value, EffectReconciliationDecision.APPLIED.value,
            }
            or effect_review.get("confirmed") is not True
            or not 20 <= len(note) <= 2000
            or variables
            or (from_step_id and from_step_id != review["step_id"])
        ):
            raise ValueError("Workflow retry is blocked: review no longer matches the failed outcome")
        # A result may arrive after the failed Agent stopped. Do not settle an
        # outstanding browser operation from a human assertion about its UI.
        if await workflow_effect_review_blocker(db, prior, applied=applied_review):
            raise ValueError("Workflow retry is blocked: a browser operation still needs reconciliation")
        if applied_review:
            schema = workflow_effect_result_schema(prior)
            if (not schema or not isinstance(effect_review.get("result"), dict)
                    or list(SchemaContractValidatorFactory.build(schema).iter_errors(effect_review["result"]))):
                raise ValueError("Workflow retry is blocked: verified result does not match the frozen output contract")
        elif effect_review.get("result") is not None:
            raise ValueError("Workflow retry is blocked: a not-applied review cannot supply a result")
        project_reviews = await workflow_released_project_reviews(db, prior, lock=True) if applied_review else {}
        if (effect_review.get("project_revisions") or {}) != {
            pid: item["revision"] for pid, item in project_reviews.items()
        }:
            raise ValueError("Workflow project changed since review; reopen the review before retrying")
        review_audit = {
            **review, "source_run_id": prior.id,
            "decision": effect_review["decision"],
            "note": note, "reviewed_by": started_by,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            **({"result": deepcopy(effect_review["result"])} if applied_review else {}),
            **({"project_checkpoints": project_reviews} if project_reviews else {}),
        }
        # A cancelled effectful Agent can have no persisted result. The
        # operator review records an explicit recovery decision without
        # inventing an external outcome for that interrupted attempt.
        reviewed_result = deepcopy((prior.step_results or {}).get(review["step_id"], {}))
        from_step_id = review["step_id"]

    def was_reviewed(step_id: str, result: dict) -> bool:
        if was_supplied(step_id):
            return True
        return bool(review_audit and step_id == review_audit["step_id"] and result == reviewed_result)

    def agent_recovery_checkpoint(step_id: str, result: dict) -> bool:
        step = source_steps.get(step_id) or {}
        return (
            step.get("type") == "agent"
            and result.get("producer_completed") is not True
            and isinstance(result.get("chrome_outcome"), dict)
        )

    def failed_effect_requires_reconciliation(step_id: str, result: dict) -> bool:
        """Block a blind retry only for the failed attempt that carries it.

        Completed predecessors can legitimately retain an external-effect
        receipt.  A failed Agent with a Chrome outcome has its own bounded
        recovery path, which re-observes before taking another action.
        """
        if result.get("status") != "failed" or agent_recovery_checkpoint(step_id, result) or was_reviewed(step_id, result):
            return False
        if provider_outage_without_external_effect(result):
            return False
        from packages.core.ai.runtime.outcome import attempt_forbids_replay

        return attempt_forbids_replay(result)

    blocked_retry = next(
        (
            (str(step_id), result)
            for step_id, result in (prior.step_results or {}).items()
            if isinstance(result, dict)
            and result.get("retry_blocked") is True
            and not agent_recovery_checkpoint(str(step_id), result)
            and not was_reviewed(str(step_id), result)
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

    unreconciled_effect = next(
        (
            (str(step_id), result)
            for step_id, result in (prior.step_results or {}).items()
            if isinstance(result, dict)
            and failed_effect_requires_reconciliation(str(step_id), result)
        ),
        None,
    )
    if unreconciled_effect is not None:
        raise ValueError(
            f"Workflow retry is blocked at step '{unreconciled_effect[0]}': its prior "
            "effect must be reconciled first"
        )

    _require_runtime_workflow(
        workflow,
        steps=execution_workflow.steps or [],
        imported_source=(
            trusted_workflow_import_source(workflow)
            if use_current
            else prior_execution_snapshot.get("import_source")
        ),
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
        if use_current:
            raise ValueError(
                f"Retry step '{retry_step_id}' is no longer part of the workflow; "
                "retry on the original definition or start a new run"
            )
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
            if snapshot is None or snapshot["fingerprint"] != source_fingerprint:
                raise ValueError("Workflow retry history has a different or missing execution snapshot")
            if (ancestor.lineage_root_run_id or ancestor.id) != (prior.lineage_root_run_id or prior.id):
                raise ValueError("Workflow retry history is outside the current lineage")
            seen_evidence_ids.add(ancestor.id)
            evidence_runs.append(ancestor)
    retry_evidence = merge_retry_evidence(source_workflow.steps or [], list(reversed(evidence_runs)))
    # Recovery cannot erase an older uncertain external effect merely because
    # a broken intermediate attempt omitted that node's receipt.
    for step_id, result in retry_evidence.executed_results.items():
        if result.get("retry_blocked") is True and not agent_recovery_checkpoint(step_id, result) and not was_reviewed(step_id, result):
            raise ValueError(f"Workflow retry is blocked at step '{step_id}': its prior effect must be reconciled first")
        if failed_effect_requires_reconciliation(step_id, result):
            raise ValueError(f"Workflow retry is blocked at step '{step_id}': its prior effect must be reconciled first")
    if review_audit:
        sid = review_audit["step_id"]
        if not was_reviewed(sid, retry_evidence.executed_results.get(sid, {})):
            raise ValueError("Workflow retry is blocked: historical effect evidence changed")
        # An applied result is an operator-attested checkpoint, never permission
        # to replay the producer. All downstream contracts still execute.
        if review_audit["decision"] == "applied":
            from packages.core.services.workflow_agent_recovery import agent_retry_context

            settled = {
                "status": "completed", "output": deepcopy(review_audit["result"]),
                "effect_state": "confirmed", "do_not_replay": True,
                "operator_effect_review": review_audit,
                "agent_recovery": agent_retry_context(
                    reviewed_result, run_id=prior.id, step_id=sid, error=prior.error,
                ),
            }
        else:
            settled = {
                **deepcopy(reviewed_result), "effect_state": "none",
                "do_not_replay": False, "retry_blocked": False,
                "operator_effect_review": review_audit,
            }
        retry_evidence.executed_results[sid] = settled
        retry_evidence.step_results[sid] = deepcopy(settled)
        # Cancelled Agents may not have saved a result/source before review.
        retry_evidence.source_run_ids[sid] = prior.id
    if supplied_audit:
        sid = supplied_audit["step_id"]
        if sid not in retry_evidence.executed_results and sid != prior.current_step_id:
            raise ValueError(f"Supplied output is for step '{sid}', which this run never reached")
        # Like an applied review, the supplied result is a completed root and
        # only what follows it runs; a later retry may run the step again.
        settled = {"status": "completed", "output": deepcopy(supplied_result), "supplied_output": supplied_audit}
        retry_evidence.executed_results[sid] = settled
        retry_evidence.step_results[sid] = deepcopy(settled)
        retry_evidence.source_run_ids[sid] = prior.id
    retry_context = build_retry_context(
        execution_workflow.steps or [], retry_evidence.step_results, retry_step_id,
        # A terminal target the resolver replaced is not the caller's checkpoint.
        explicit_checkpoint=bool(from_step_id) and retry_step_id == str(from_step_id).strip(),
        added_step_ids=set(workflow_steps) - set(source_steps) if use_current else (),
    )
    accepted_step_id = (
        review_audit["step_id"] if review_audit and review_audit["decision"] == "applied"
        else supplied_audit["step_id"] if supplied_audit else None
    )
    if accepted_step_id:
        # Traverse the accepted node as a completed root; only its downstream
        # frontier is runnable. This also supports an Agent that ends the graph.
        sid = accepted_step_id
        retry_context["invalidated_step_ids"].remove(sid)
        retry_context["inherited_step_ids"].append(sid)
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
    stale_project = await _stale_inherited_workflow_project(
        db, prior, workflow_steps, inherited_results, retry_context["invalidated_step_ids"],
    )
    if stale_project is not None:
        raise ValueError(
            "This run can't be retried: a newer version was made after it. "
            "Start from the latest version instead."
        )
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
        progress = await _retry_production_progress(db, prior, execution_workflow.steps or [], recovery_step_id)
        if progress is not None:
            inherited_results[recovery_step_id]["production_progress"] = progress
        if review_audit and recovery_step_id == review_audit["step_id"] and recovery is not None:
            recovery["operator_effect_review"] = deepcopy(review_audit)
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
    if review_audit:
        retry_trigger["_workflow_effect_review"] = review_audit
    if supplied_audit:
        retry_trigger[SUPPLIED_OUTPUT_KEY] = supplied_audit
    # A revision is a new version, not a recovery: what writes per attempt
    # (a project's take folder) must not reuse the version it revises.
    retry_trigger.pop("_workflow_revision", None)
    if revision:
        retry_trigger["_workflow_revision"] = {"of_run_id": prior.id}
    prior_runtime_context = prior_trigger.get("_workflow_runtime_context")
    if isinstance(prior_runtime_context, dict):
        # Retry is a server-owned continuation of this durable run. Preserve
        # its frozen Runtime scope after public trigger sanitization above.
        retry_trigger["_workflow_runtime_context"] = deepcopy(prior_runtime_context)
    retry_execution_snapshot = (
        build_execution_snapshot(workflow, fingerprint=current_fingerprint)
        if use_current
        else deepcopy(prior_execution_snapshot)
    )
    retry_trigger[RETRY_CONTEXT_KEY] = {
        **retry_context,
        "fingerprint": retry_execution_snapshot["fingerprint"],
        "source_run_ids": {sid: retry_evidence.source_run_ids[sid] for sid in inherited_ids},
        # Receipts from an earlier definition, reused at the checkpoint.
        **({"definition": "current", "source_fingerprint": source_fingerprint} if use_current else {}),
    }
    if user_requested and acting_user_id:
        retry_trigger["_workspace_user_request"] = {"user_id": acting_user_id}
    if retried_for_starter:
        retry_trigger[RETRY_REQUESTED_BY_KEY] = {"user_id": started_by}
    if prior_snapshot and not use_current:
        retry_trigger.update(_snapshot_display_metadata(prior_snapshot))
    await _name_run_after_binding(db, retry_trigger, prior.binding_id)
    retry_trigger["input_patch"] = deepcopy(variables or {})
    retry_variables = _retry_variables(
        execution_workflow,
        retry_evidence,
        inherited_ids,
        normalized_variables,
        retry_step_id,
        retry_step_ids=set(retry_context["entry_step_ids"]),
        source_steps=list(source_steps.values()) if use_current else None,
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
            if prior_snapshot and not use_current
            else build_definition_snapshot(
                execution_workflow,
                fingerprint=retry_execution_snapshot["fingerprint"],
            )
        ),
        execution_snapshot=retry_execution_snapshot,
        execution_trace=[],
        started_by=acting_user_id,
        started_at=datetime.now(timezone.utc),
    )
    await _refresh_completed_foreach_retries(db, retry, workflow_steps)
    await _transfer_retry_project_leases(db, retry, workflow_steps, inherited_ids)
    db.add(retry)
    await db.flush()
    await db.refresh(retry)
    return retry


async def _refresh_completed_foreach_retries(
    db: AsyncSession, retry: WorkflowRun, steps: dict[str, dict],
) -> None:
    """Reuse a completed child retry when its parent retries the map barrier."""
    for step_id, step in steps.items():
        if step.get("type") != "foreach_subworkflow":
            continue
        progress = retry.variables.get(f"__foreach_subworkflow__{step_id}")
        if not isinstance(progress, dict):
            continue
        for item in progress.get("items") or []:
            if item.get("status") not in {"failed", "cancelled"} or not item.get("subrun_id"):
                continue
            child = await db.get(WorkflowRun, item["subrun_id"])
            trigger = child.trigger_data or {} if child else {}
            parent = await db.get(WorkflowRun, trigger.get("parent_run_id")) if trigger.get("parent_run_id") else None
            if (child is None or parent is None or child.lineage_is_legacy is not False
                    or child.entity_id != retry.entity_id or child.workspace_id != retry.workspace_id
                    or parent.entity_id != retry.entity_id or parent.workspace_id != retry.workspace_id
                    or (parent.lineage_root_run_id or parent.id) != retry.lineage_root_run_id
                    or trigger.get("parent_step_id") != step_id
                    or trigger.get("parent_foreach_item_key") != item.get("key")):
                continue
            latest = await latest_workflow_run_family_attempt(db, child)
            if (latest.id == child.id or latest.status != "completed"
                    or latest.workflow_id != child.workflow_id or latest.workspace_id != child.workspace_id
                    or latest.started_by != child.started_by or latest.lineage_is_legacy is not False
                    or latest.execution_snapshot != child.execution_snapshot):
                continue
            item.update(status="completed", subrun_id=latest.id, output=deepcopy(latest.variables or {}), error=None)


async def _transfer_retry_project_leases(
    db: AsyncSession, retry: WorkflowRun, steps: dict[str, dict], inherited_ids: set[str],
) -> None:
    """Move outstanding native leases with the accepted retry checkpoint."""
    from packages.core.services.workflow_project_service import _state_value, _set_state_value

    def refresh_project_projection(
        value: object,
        *,
        project_id: str,
        prior_revision: int,
        refreshed: dict,
        legacy_handoff_projection: Callable[[dict], bool] | None = None,
    ) -> object:
        """Refresh only retry snapshots made stale by this lease handoff.

        A retry is allowed to carry prior node outputs.  Once this function has
        verified and transferred a lease, every standard project projection of
        that same, just-transferred revision is stale by exactly one revision.
        Do not recursively rewrite arbitrary nested state and do not touch a
        projection from another project or revision: those may represent a
        genuine concurrent update and must retain normal CAS protection.
        """
        if not isinstance(value, dict):
            return value
        # A project projection is the public output shape of the
        # ``workflow_project`` node.  Do not rewrite arbitrary caller data
        # which happens to use similarly named fields.
        is_project_projection = (
            isinstance(value.get("project_id"), str)
            and isinstance(value.get("revision"), int)
            and isinstance(value.get("state"), dict)
            and isinstance(value.get("project_type"), str)
            and isinstance(value.get("schema_version"), int)
            and isinstance(value.get("current_stage"), str)
        )
        if (
            is_project_projection
            and value.get("project_id") == project_id
            and (
                value.get("revision") == prior_revision
                or (legacy_handoff_projection is not None and legacy_handoff_projection(value))
            )
        ):
            return {**value, **deepcopy(refreshed)}
        project_value = value.get("project")
        if isinstance(project_value, dict):
            updated_project = refresh_project_projection(
                project_value,
                project_id=project_id,
                prior_revision=prior_revision,
                refreshed=refreshed,
                legacy_handoff_projection=legacy_handoff_projection,
            )
            if updated_project is not project_value:
                return {**value, "project": updated_project}
        return value

    def refresh_retry_project_projections(
        *, project_id: str, prior_revision: int, refreshed: dict,
        legacy_handoff_projection: Callable[[dict], bool] | None = None,
    ) -> None:
        retry.variables = {
            key: refresh_project_projection(
                value,
                project_id=project_id,
                prior_revision=prior_revision,
                refreshed=refreshed,
                legacy_handoff_projection=legacy_handoff_projection,
            )
            for key, value in retry.variables.items()
        }
        for inherited_step_id, inherited_result in retry.step_results.items():
            if not isinstance(inherited_result, dict) or "output" not in inherited_result:
                continue
            output = inherited_result["output"]
            updated_output = refresh_project_projection(
                output,
                project_id=project_id,
                prior_revision=prior_revision,
                refreshed=refreshed,
                legacy_handoff_projection=legacy_handoff_projection,
            )
            if updated_output is not output:
                retry.step_results[inherited_step_id] = {
                    **inherited_result,
                    "output": updated_output,
                }

    def is_current_owner_projection(
        value: object,
        *,
        project: WorkflowProject,
        owner: WorkflowRun,
        revision: int | None = None,
        current_stage: str | None = None,
    ) -> bool:
        return (
            isinstance(value, dict)
            and value.get("project_id") == project.id
            and value.get("revision") == (project.revision if revision is None else revision)
            and value.get("state") == project.state
            and value.get("last_run_id") == owner.id
            and value.get("project_type") == project.project_type
            and value.get("schema_version") == project.schema_version
            and value.get("current_stage") == (
                project.current_stage if current_stage is None else current_stage
            )
        )

    def owner_proved_current_project(
        project: WorkflowProject,
        owner: WorkflowRun,
        *,
        revision: int | None = None,
        current_stage: str | None = None,
    ) -> bool:
        for owner_step_id, owner_result in (owner.step_results or {}).items():
            if (steps.get(owner_step_id, {}).get("type") != "workflow_project"
                    or not isinstance(owner_result, dict) or owner_result.get("status") != "completed"):
                continue
            output = owner_result.get("output")
            if is_current_owner_projection(
                output,
                project=project,
                owner=owner,
                revision=revision,
                current_stage=current_stage,
            ):
                return True
            if isinstance(output, dict) and is_current_owner_projection(
                output.get("project"),
                project=project,
                owner=owner,
                revision=revision,
                current_stage=current_stage,
            ):
                return True
        return False

    completed = set()
    for step_id in inherited_ids:
        step = steps[step_id]
        config = step.get("config") or {}
        result = retry.step_results.get(step_id) or {}
        output = result.get("output") or {}
        if (step.get("type") == "workflow_project" and config.get("operation") == "complete_lease"
                and result.get("status") == "completed" and isinstance(output, dict)):
            completed.add((output.get("project_id"), config.get("lease_path") or "lease"))

    for step_id in sorted(inherited_ids):
        step = steps[step_id]
        config = step.get("config") or {}
        result = retry.step_results.get(step_id) or {}
        if (step.get("type") != "workflow_project" or config.get("operation") != "lease_due"
                or result.get("status") != "completed"):
            continue
        output = result.get("output") or {}
        receipt = output.get("project") if isinstance(output, dict) else None
        if not isinstance(receipt, dict):
            continue
        project_id = receipt.get("project_id")
        lease_path = config.get("lease_path") or "lease"
        if (project_id, lease_path) in completed:
            continue
        expected = _state_value(receipt.get("state") or {}, lease_path)
        project = await db.scalar(select(WorkflowProject).where(
            WorkflowProject.id == project_id, WorkflowProject.entity_id == retry.entity_id,
            WorkflowProject.workspace_id == retry.workspace_id,
        ).with_for_update().execution_options(populate_existing=True))
        lease = _state_value(project.state or {}, lease_path) if project else None
        review = (retry.trigger_data or {}).get("_workflow_effect_review") or {}
        released_checkpoint = (review.get("project_checkpoints") or {}).get(project_id) or {}
        released_review = (
            review.get("decision") == "applied" and project is not None
            and project.current_stage == "verification_required" and lease == {}
            and released_checkpoint.get("revision") == project.revision
            and released_checkpoint.get("lease_path") == lease_path
            and isinstance(expected, dict) and expected.get("run_id") == review.get("source_run_id")
        )
        retained_verification_lease = (
            project is not None
            and project.current_stage == "verification_required"
            and isinstance(expected, dict)
            and project.last_run_id == expected.get("run_id")
        )
        # A project the failed owner put on hold (a provider outage) is retried from that owner's lease.
        held_by_owner = (
            project is not None and project.current_stage == "held"
            and isinstance(expected, dict) and project.last_run_id == expected.get("run_id")
        )
        # The failed owner released its lease back to the stage it waited in
        # (release_failed_run_project_leases): the retry leases that stage again.
        released_failure = (
            project is not None and isinstance(expected, dict)
            and project.current_stage == expected.get("pending_stage")
            and project.current_stage != "leased" and lease == {}
            and project.last_run_id == expected.get("run_id")
        )
        if (project is None or (project.current_stage not in {"leased", "verification_required"}
                                and not held_by_owner and not released_failure)
                or not isinstance(expected, dict) or not expected.get("run_id")
                or not isinstance(lease, dict)
                or (lease != expected and not released_review and not released_failure)
                or (project.current_stage == "verification_required" and not retained_verification_lease
                    and not released_review and not released_failure)):
            raise ValueError("Workflow project lease changed; reconcile it before retrying")
        owner = await db.get(WorkflowRun, expected["run_id"])
        if (owner is None or owner.entity_id != retry.entity_id
                or owner.workspace_id != retry.workspace_id or owner.workflow_id != retry.workflow_id
                or (owner.lineage_root_run_id or owner.id) != retry.lineage_root_run_id
                or owner.status not in {"failed", "cancelled"}):
            raise ValueError("Workflow project lease is not owned by a terminal retry ancestor")
        retained_verification_lease_is_current_owner_state = (
            retained_verification_lease
            and owner_proved_current_project(
                project,
                owner,
                revision=project.revision - 1,
                current_stage="leased",
            )
        )
        if (project.current_stage == "verification_required"
                and not retained_verification_lease_is_current_owner_state
                and not released_review and not released_failure):
            raise ValueError("Workflow project lease changed; reconcile it before retrying")
        owner_proves_handoff_state = owner_proved_current_project(project, owner)
        handoff_state = deepcopy(project.state)
        state = deepcopy(project.state)
        handoff_revision = project.revision
        _set_state_value(state, lease_path, {
            **(expected if released_review or released_failure else lease), "run_id": retry.id,
            "expires_at": (datetime.now(timezone.utc) + timedelta(
                seconds=int(config.get("lease_seconds") or 900),
            )).isoformat(),
        })
        project.state, project.current_stage, project.last_run_id = state, "leased", retry.id
        project.revision += 1
        refreshed = {
            **receipt,
            "state": deepcopy(state),
            "revision": project.revision,
            "last_run_id": retry.id,
            "current_stage": project.current_stage,
        }
        predecessor_id = owner.effective_retry_of_run_id
        predecessor = await db.get(WorkflowRun, predecessor_id) if predecessor_id else None

        def is_legacy_handoff_projection(candidate: dict) -> bool:
            """Accept only the immediately preceding retry-owned lease view.

            Older retry records can contain an alias that an earlier server
            did not refresh.  It is safe to repair exactly one such handoff
            only when the whole project state proves no business write occurred
            between the old owner and the currently locked lease.
            """
            if (
                not owner_proves_handoff_state
                or candidate.get("revision") != handoff_revision - 1
                or candidate.get("last_run_id") != predecessor_id
                or candidate.get("project_type") != project.project_type
                or candidate.get("schema_version") != project.schema_version
                or candidate.get("current_stage") != project.current_stage
                or predecessor is None
                or predecessor.entity_id != retry.entity_id
                or predecessor.workspace_id != retry.workspace_id
                or predecessor.workflow_id != retry.workflow_id
                or (predecessor.lineage_root_run_id or predecessor.id) != retry.lineage_root_run_id
                or predecessor.status not in {"failed", "cancelled"}
            ):
                return False
            candidate_lease = _state_value(candidate.get("state") or {}, lease_path)
            if not isinstance(candidate_lease, dict) or candidate_lease.get("run_id") != predecessor_id:
                return False
            handoff_fields = {"run_id", "expires_at"}
            if ({key: value for key, value in candidate_lease.items() if key not in handoff_fields}
                    != {key: value for key, value in expected.items() if key not in handoff_fields}):
                return False
            candidate_state = deepcopy(candidate["state"])
            _set_state_value(candidate_state, lease_path, deepcopy(expected))
            return candidate_state == handoff_state

        def is_retained_verification_projection(candidate: dict) -> bool:
            return (
                retained_verification_lease_is_current_owner_state
                and candidate.get("revision") == handoff_revision - 1
                and candidate.get("last_run_id") == owner.id
                and candidate.get("project_type") == project.project_type
                and candidate.get("schema_version") == project.schema_version
                and candidate.get("current_stage") == "leased"
                and candidate.get("state") == handoff_state
            )

        def is_released_failure_projection(candidate: dict) -> bool:
            """The owner's view of its lease, from just before the failure released it."""
            def with_lease(state: dict) -> dict:
                moved = deepcopy(state)
                _set_state_value(moved, lease_path, deepcopy(expected))
                return moved

            return (
                released_failure
                and candidate.get("revision") == handoff_revision - 1
                and candidate.get("last_run_id") == owner.id
                and candidate.get("project_type") == project.project_type
                and candidate.get("schema_version") == project.schema_version
                and candidate.get("current_stage") == "leased"
                and isinstance(candidate.get("state"), dict)
                and with_lease(candidate["state"]) == with_lease(handoff_state)
            )

        def is_refreshable_handoff_projection(candidate: dict) -> bool:
            return (
                is_retained_verification_projection(candidate)
                or is_legacy_handoff_projection(candidate)
                or is_released_failure_projection(candidate)
                or (released_review
                    and candidate.get("revision") == released_checkpoint.get("checkpoint_revision")
                    and candidate.get("last_run_id") == owner.id
                    and candidate.get("project_type") == project.project_type
                    and candidate.get("schema_version") == project.schema_version
                    and candidate.get("current_stage") == "leased"
                    and any(
                        isinstance(result, dict) and result.get("status") == "completed"
                        and steps.get(sid, {}).get("type") == "workflow_project"
                        and (result.get("output") == candidate or
                             isinstance(result.get("output"), dict) and result["output"].get("project") == candidate)
                        for sid, result in (owner.step_results or {}).items()
                    ))
            )

        # The lease receipt itself is an authority-bearing projection even if
        # later nodes patched the project before this retry.  Its ownership
        # and state were checked above under the project row lock, so it may
        # move with the lease.  Other projections must have observed the
        # durable project at this handoff revision to be refreshed below.
        if (owner_proves_handoff_state or retained_verification_lease_is_current_owner_state
                or released_review or released_failure):
            retry.step_results[step_id] = {
                **result,
                "output": {**output, "project": deepcopy(refreshed)},
            }
            output_var = str(config.get("output_var") or step_id)
            variable_output = retry.variables.get(output_var)
            if isinstance(variable_output, dict):
                retry.variables[output_var] = {
                    **variable_output,
                    "project": deepcopy(refreshed),
                }
        refresh_retry_project_projections(
            project_id=project_id,
            prior_revision=handoff_revision,
            refreshed=refreshed,
            legacy_handoff_projection=is_refreshable_handoff_projection,
        )


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
    include_unbound: bool = False,
    app_action: str | None = None,
    include_unlabelled: bool = False,
) -> list[WorkflowRun]:
    q = select(WorkflowRun).where(
        WorkflowRun.entity_id == entity_id,
        _NOT_A_NODE_TEST,
    )
    if app_action:
        # One Flow can back several App entries; each lists the runs it started.
        started_from = WorkflowRun.trigger_data[WORKSPACE_APP_ACTION_KEY].as_string()
        q = q.where(or_(started_from == app_action, started_from.is_(None)) if include_unlabelled
                    else started_from == app_action)
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
    if include_unbound:
        if not binding_id or not workflow_id:
            raise ValueError("Unbound history requires a binding and workflow")
        q = q.where(or_(
            and_(
                WorkflowRun.binding_id == binding_id,
                WorkflowRun.workflow_id == workflow_id,
            ),
            and_(
                WorkflowRun.binding_id.is_(None),
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.trigger_source == "manual",
            ),
        ))
    elif workflow_id:
        q = q.where(WorkflowRun.workflow_id == workflow_id)
    if workspace_id:
        q = q.where(WorkflowRun.workspace_id == workspace_id)
    if binding_id and not include_unbound:
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
    runner = WorkflowRunner()
    try:
        result = await runner._settle_failure(step, await runner._execute_step_safe(step, run, db), run, db)
    finally:
        await db.refresh(run)
        if run.status == "running":
            run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
    if result.get("status") == "failed":
        run.status = "failed"
        run.error = workflow_run_failure_headline(step, result, fallback="Node test failed")
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

    lost_claim_token: str | None = None
    try:
        async with workflow_run_execution_claim(run_id) as claim:
            if not claim:
                return {"error": "Run is already executing"}
            lost_claim_token = getattr(claim, "token", None)
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
        settled_status = await WorkflowRunner._recover_lost_execution_claim(
            run_id,
            lost_claim_token=lost_claim_token,
        )
        if settled_status is None:
            return {
                "status": "running",
                "error": "Workflow execution ownership changed; the current owner is still running",
            }
        if settled_status in {"pending", "running"}:
            return {
                "status": settled_status,
                "error": None,
                "interrupted": True,
                "message": "The step was interrupted before it finished; run the step again.",
            }
        run = await get_run(db, run_id, entity_id)
        return {
            "status": settled_status,
            "error": run.error if run is not None and settled_status == "failed" else None,
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
        from packages.core.ai.workflow_runner import (
            _no_runnable_steps_error,
            _unapplied_step_outcome,
        )

        unapplied = _unapplied_step_outcome(execution_workflow.steps or [], run)
        if unapplied is None:
            return {"error": _no_runnable_steps_error(execution_workflow.steps or [], run)}
        # An interrupted owner committed this node's receipt but not the run's
        # outcome; settle it the way that owner would have.
        step, result = unapplied
        run.current_step_id = step["id"]
        if result.get("status") == "paused":
            run.status = "paused"
            await db.flush()
            return {"status": "paused", "step_id": step["id"]}
        run.status = "failed"
        run.error = workflow_run_failure_headline(step, result)
        run.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return {"status": "failed", "step_id": step["id"], "error": run.error}

    step = runnable[0]
    from packages.core.services.workflow_claim_recovery import (
        MANUAL_STEP_EXECUTION_DRIVER,
        WORKFLOW_EXECUTION_DRIVER_TRIGGER_FIELD,
    )

    trigger_data = dict(run.trigger_data or {})
    if trigger_data.get(WORKFLOW_EXECUTION_DRIVER_TRIGGER_FIELD) != MANUAL_STEP_EXECUTION_DRIVER:
        # Claim-loss recovery must leave a stepped run for its next manual
        # step rather than hand the rest of the graph to a worker.
        trigger_data[WORKFLOW_EXECUTION_DRIVER_TRIGGER_FIELD] = MANUAL_STEP_EXECUTION_DRIVER
        run.trigger_data = trigger_data
    run.current_step_id = step["id"]
    append_execution_trace(run, node=step, status="running")
    # Same pre-execution checkpoint as the full runner: recovery can only tell
    # an interrupted node from an unstarted one by this durable transition.
    await db.commit()
    result = await runner._settle_failure(step, await runner._execute_step_safe(step, run, db), run, db)
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
        run.error = workflow_run_failure_headline(step, result)
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
