"""blueprint payload → new workspace.

Two install modes:

  ``simulate``  Sets ``settings.sandbox=true`` so the M3 sandbox
                machinery takes over — plans default to ``dry_run``,
                measurements are simulated, no real channel sends.
                The blueprint payload is preserved verbatim under
                ``settings._blueprint`` so ``promote_workspace`` later
                knows what to flip back to.

  ``live``      Real install. Operator is on the hook for cost +
                consequences immediately.

Both modes:

  * Create the workspace
  * Resolve agent_slug → Agent (skip subscriptions whose agent isn't
    available unless ``create_missing_agents=true``)
  * Apply governance policy (writes a revision row)
  * Create custom field definitions
  * Create stats and goals (measurement schedules follow the Workspace
    runtime switch)
  * Create scheduled jobs
  * Bind explicitly selected channel accounts to installed services; never
    create credentials or browser sessions. Unresolved setup stays a to-do.

Returns ``InstallResult`` so the caller can render a summary card
("workspace created, 3 goals, 2 jobs, 4 things you need to wire up
before this works for real").
"""
from __future__ import annotations

from collections.abc import Mapping

import hashlib
import json
import math
import re
from copy import deepcopy

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import and_, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.knowledge_templates import (
    INITIAL_BODY_KEY,
    normalize_knowledge_template,
)
from packages.core.constants.agents import is_master_agent
from packages.core.constants.blueprints import (
    BlueprintInstallTodoKind,
    installed_blueprint_job_id,
)
from packages.core.blueprints.freshness import (
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.blueprints.payload import (
    PayloadError, migrate_payload, validate_payload,
)
from packages.core.blueprints.registry import sync_installed_package
from packages.core.blueprints.simulation import resolve_simulation_experience
from packages.core.blueprints.workflow_dependencies import (
    WorkflowDependencyError,
    WorkflowDependencyFactory,
    WorkflowSkillDependencyFactory,
    WorkflowDocumentDependencyFactory,
)
from packages.core.governance import WorkspacePolicy, update_policy
from packages.core.models.base import generate_ulid
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import (
    Document,
    DocumentFolder,
    DocumentGroup,
    DocumentGroupMember,
    VectorStatus,
)
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.permission import Visibility
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.agent_runtime_config import normalize_agent_runtime_config
from packages.core.services.entity_service import create_workspace
from packages.core.services.integration_resolution import is_user_facing_integration_provider
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
    RELATIONSHIP_INSTALLED_COMPONENT,
    RELATIONSHIP_INSTALLED_FROM,
    RESOURCE_AGENT,
    RESOURCE_SKILL,
    RESOURCE_WORKFLOW,
    RESOURCE_WORKSPACE,
    RESOURCE_WORKSPACE_BLUEPRINT,
    SCOPE_WORKSPACE,
    get_marketplace_resource_link,
    record_marketplace_resource_link,
)
from packages.core.services.resource_access import (
    ResourceDescriptor,
    readable_resource_ids,
)
from packages.core.services.reusable_resource_locks import (
    lock_agent_skill_binding_references,
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_payload_references,
    lock_reusable_resource_reference,
    lock_reusable_resource_references,
    reusable_resource_ids_from_payload,
)
from packages.core.services.workspace_access import (
    ensure_workspace_owner_membership,
    settings_with_default_workspace_access,
)

logger = logging.getLogger(__name__)

# Compatibility version for the Blueprint install/runtime contract. This is
# intentionally independent from the Python package's pre-1.0 release number:
# built-in Blueprints declare the public Manor runtime contract (currently 1.0).
BLUEPRINT_INSTALL_RUNTIME_VERSION = "1.0"

_BLUEPRINT_WORKFLOW_KIND_TO_TYPE = {
    "agent_call": "agent",
    "tool_call": "tool",
    "hitl_approval": "wait",
}


def blueprint_workflow_installation_source_id(
    source_blueprint_id: str,
    workspace_id: str,
) -> str:
    """Stable internal mapping id for one Blueprint install scope.

    Marketplace ids remain the only external source identity. This digest is
    solely the unique key used by the legacy WorkflowTemplateInstallation
    table, whose schema is entity-scoped and otherwise aliases the same Flow
    component across every Workspace that installs a Blueprint.
    """
    source = str(source_blueprint_id or "").strip()
    scope = str(workspace_id or "").strip()
    if not source or not scope:
        raise ValueError("Blueprint source id and Workspace id are required")
    digest = hashlib.sha256(f"{source}\0{scope}".encode()).hexdigest()
    return f"workspace-blueprint-install:{digest}"


class InstallError(Exception):
    """Raised when a blueprint can't be installed (malformed payload,
    missing required agents, etc.)."""


class InstallMode(str, Enum):
    SIMULATE = "simulate"
    LIVE = "live"


@dataclass
class InstallTodo:
    """One unmet requirement the operator needs to address before the
    workspace is fully functional."""

    kind: str
    """'channel' | 'browser_session' | 'missing_agent' | 'note'"""
    detail: str
    """Operator-readable description of what to do."""
    payload: dict[str, Any] = field(default_factory=dict)
    """Machine-readable details — drives the UI's deep-link buttons."""
    blocking: bool = True
    """If False, the workspace can run without this — surfaces as a
    soft warning instead of a red flag."""


def author_hard_blocks(payload: Mapping[str, Any] | None) -> list[str]:
    """The hard-block scopes shipped in either policy or operating rules."""

    policy = payload.get("policy") if isinstance(payload, Mapping) else None
    governance = policy.get("governance") if isinstance(policy, Mapping) else None
    out: list[str] = []
    for key in ("never_allow_actions", "never_allow_capabilities"):
        for value in (governance.get(key) or []) if isinstance(governance, Mapping) else []:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    from packages.core.governance.approval_projection import (
        compile_workspace_rules, policy_override, selector_label,
    )
    if isinstance(governance, dict) and "approval" in governance:
        for rule in policy_override(governance).rules:
            if rule.effect == "deny" and selector_label(rule) not in out:
                out.append(selector_label(rule))
    recipe = payload.get("recipe") if isinstance(payload, Mapping) else None
    operating_model = recipe.get("operating_model") if isinstance(recipe, Mapping) else None
    rules = operating_model.get("rules") if isinstance(operating_model, Mapping) else None
    if isinstance(rules, list):
        from packages.core.services.workspace_setup_service import _infer_rule_enforcement

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("approval_selectors") is not None:
                for compiled in compile_workspace_rules([rule]):
                    if compiled.effect == "deny" and selector_label(compiled) not in out:
                        out.append(selector_label(compiled))
                continue
            enforcement = _infer_rule_enforcement(rule)
            if enforcement and enforcement["effect"] == "deny":
                for pattern in enforcement["patterns"] + enforcement["capability_patterns"]:
                    if pattern not in out:
                        out.append(pattern)
    return out


def demote_author_hard_blocks(policy: WorkspacePolicy) -> tuple[WorkspacePolicy, list[str]]:
    """Compatibility helper for policy-only author defaults; input is unchanged."""
    from packages.core.governance.defaults import normalize_default_workspace_governance

    _, projected, moved = normalize_default_workspace_governance([], policy)
    return projected, moved


@dataclass
class InstallResult:
    workspace_id: str
    mode: InstallMode
    blueprint_id: Optional[str]
    blueprint_slug: Optional[str]
    stat_ids: list[str] = field(default_factory=list)
    goal_ids: list[str] = field(default_factory=list)
    subscription_ids: list[str] = field(default_factory=list)
    scheduled_job_ids: list[str] = field(default_factory=list)
    workflow_binding_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    custom_field_ids: list[str] = field(default_factory=list)
    governance_applied: bool = False
    #: Author never_allow entries installed as approval gates instead (the
    #: operator did not opt in to the author's hard blocks).
    demoted_hard_blocks: list[str] = field(default_factory=list)
    todos: list[InstallTodo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def validate_portable_recipe_resources(recipe: Mapping[str, Any]) -> None:
    """Validate Draft/Blueprint executable resources before materialization."""
    from packages.core.blueprints.payload import _validate_scheduled_jobs, _validate_workflows
    portable = {"workflows": list(recipe.get("workflows") or []), "scheduled_jobs": list(recipe.get("scheduled_jobs") or [])}
    _validate_workflows(portable)
    _validate_scheduled_jobs(portable)
    for workflow in portable["workflows"]:
        if isinstance(workflow, dict):
            _blueprint_workflow_definition_values(workflow)


async def materialize_workspace_recipe_resources(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace: Workspace,
    recipe: Mapping[str, Any],
    user_id: str | None,
    mode: InstallMode = InstallMode.LIVE,
    source_template_id: str,
    source_version: str = "1.0.0",
    binding_source_template_id: str | None = None,
    skill_id_by_component_key: dict[str, str] | None = None,
) -> dict[str, dict[str, str] | list[str]]:
    """Materialize portable Workflow/Job resources on an existing Workspace.

    Direct Blueprint install and the AI Draft path both call this primitive.
    It deliberately receives an existing Workspace and performs no lifecycle,
    transaction, capacity, approval, or post-commit dispatch work itself.
    """
    workflow_specs = [dict(item) for item in recipe.get("workflows") or [] if isinstance(item, Mapping)]
    stat_ids: list[str] = []
    stat_ids_by_key: dict[str, str] = {}
    for stat_payload in recipe.get("stats") or []:
        if not isinstance(stat_payload, Mapping):
            raise InstallError("Blueprint stats must be objects")
        key = str(stat_payload.get("key") or stat_payload.get("library_key") or "").strip()
        stat_id = await _install_stat(
            db, entity_id=entity_id, workspace_id=workspace.id,
            payload=dict(stat_payload), mode=mode,
        )
        stat_ids.append(stat_id)
        if key:
            stat_ids_by_key[key] = stat_id

    workflow_ids: dict[str, str] = {}
    binding_ids: dict[str, str] = {}
    for spec in workflow_specs:
        workflow_id = await _install_workflow(
            db, entity_id=entity_id, workspace_id=workspace.id, w=spec,
            source_template_id=source_template_id, source_version=source_version,
            installed_by=user_id, adopt_unmapped=False,
        )
        if workflow_id:
            workflow_ids[str(spec.get("slug") or "").strip()] = workflow_id
    for spec in workflow_specs:
        slug = str(spec.get("slug") or "").strip()
        workflow_id = workflow_ids.get(slug)
        resolved = spec
        if WorkflowDependencyFactory.has_dependencies(list(spec.get("steps") or [])):
            try:
                resolved = deepcopy(spec)
                resolved["steps"] = WorkflowDependencyFactory.to_runtime(
                    list(spec.get("steps") or []), workflow_id_by_key=workflow_ids,
                )
            except WorkflowDependencyError as exc:
                raise InstallError(str(exc)) from exc
            workflow_id = await _install_workflow(
                db, entity_id=entity_id, workspace_id=workspace.id, w=resolved,
                source_template_id=source_template_id, source_version=source_version,
                installed_by=user_id, adopt_unmapped=False,
            )
            if workflow_id:
                workflow_ids[slug] = workflow_id
        if workflow_id and not bool(spec.get("internal")):
            binding_id = await _install_workflow_binding(
                db, entity_id=entity_id, workspace_id=workspace.id,
                workflow_id=workflow_id, w=resolved,
                source_template_id=binding_source_template_id or source_template_id,
            )
            if binding_id:
                binding_ids[slug] = binding_id

    job_ids: list[str] = []
    for job in recipe.get("scheduled_jobs") or []:
        if not isinstance(job, Mapping):
            raise InstallError("Blueprint scheduled jobs must be objects")
        job_ids.append(await _install_scheduled_job(
            db, entity_id=entity_id, workspace_id=workspace.id, sj=dict(job),
            user_id=user_id, mode=mode, source_template_id=source_template_id,
            skill_id_by_component_key=skill_id_by_component_key,
        ))
    return {
        "stat_ids": stat_ids, "stat_ids_by_key": stat_ids_by_key,
        "workflow_ids": workflow_ids, "workflow_binding_ids": binding_ids,
        "scheduled_job_ids": job_ids,
    }


_INSTALL_VARIABLE_PATTERN = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]{0,99})\s*\}\}"
)


class _BlueprintVariableValueKind(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


def _blueprint_variable_value_kind(value: Any) -> _BlueprintVariableValueKind | None:
    """Return the JSON value kind used by a Blueprint variable declaration."""

    if isinstance(value, bool):
        return _BlueprintVariableValueKind.BOOLEAN
    if isinstance(value, str):
        return _BlueprintVariableValueKind.STRING
    if isinstance(value, (int, float)):
        return _BlueprintVariableValueKind.NUMBER
    if isinstance(value, list):
        return _BlueprintVariableValueKind.ARRAY
    if isinstance(value, dict):
        return _BlueprintVariableValueKind.OBJECT
    return None


def _validate_install_variable_type(
    *,
    key: str,
    declaration: dict[str, Any],
    value: Any,
) -> None:
    """Keep API callers on the same typed contract as the install UI."""

    if "default" not in declaration or declaration.get("default") is None:
        return
    expected = _blueprint_variable_value_kind(declaration.get("default"))
    actual = _blueprint_variable_value_kind(value)
    if expected is None:
        return
    if expected is not actual:
        actual_label = actual.value if actual is not None else "null"
        raise InstallError(
            f"Blueprint install variable {key!r} must be {expected.value}; "
            f"got {actual_label}"
        )
    if (
        actual is _BlueprintVariableValueKind.NUMBER
        and isinstance(value, float)
        and not math.isfinite(value)
    ):
        raise InstallError(
            f"Blueprint install variable {key!r} must be a finite number"
        )


def _compat_version(value: object, *, field_name: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", text):
        raise InstallError(f"{field_name} must be a numeric semantic version")
    parts = [int(part) for part in text.split(".")]
    return tuple((parts + [0, 0])[:3])  # type: ignore[return-value]


def _assert_runtime_compatible(payload: dict[str, Any]) -> None:
    required = str(
        (((payload.get("contract") or {}).get("requires") or {}).get(
            "manor_min_version"
        ))
        or ""
    ).strip()
    if not required:
        return
    if _compat_version(
        BLUEPRINT_INSTALL_RUNTIME_VERSION,
        field_name="Blueprint installer runtime version",
    ) < _compat_version(required, field_name="contract.requires.manor_min_version"):
        raise InstallError(
            "Blueprint requires Manor "
            f"{required} or newer; this installer supports "
            f"{BLUEPRINT_INSTALL_RUNTIME_VERSION}"
        )


def resolve_install_variables(
    payload: dict[str, Any],
    supplied: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply declared values and return the safe materialized subset to persist."""

    declarations = {
        str(item.get("key")): item
        for item in (payload.get("contract") or {}).get("variables") or []
        if isinstance(item, dict) and item.get("key")
    }
    values = dict(supplied or {})
    unknown = sorted(set(values) - set(declarations))
    if unknown:
        raise InstallError(f"unknown Blueprint install variables: {unknown}")

    resolved: dict[str, Any] = {}
    for key, declaration in declarations.items():
        if key in values:
            value = values[key]
            _validate_install_variable_type(
                key=key,
                declaration=declaration,
                value=value,
            )
        elif "default" in declaration:
            value = declaration.get("default")
        else:
            value = None
        missing_required = value is None or (
            isinstance(value, str) and not value.strip()
        )
        if missing_required and bool(declaration.get("required", False)):
            raise InstallError(f"required Blueprint install variable {key!r} is missing")
        if value is not None:
            resolved[key] = value

    def replace(node: Any, *, path: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            return {
                key: replace(
                    value,
                    path=(*path, str(key)),
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                replace(item, path=(*path, str(index)))
                for index, item in enumerate(node)
            ]
        inside_variable_declarations = path[:2] == ("contract", "variables")
        if not isinstance(node, str) or inside_variable_declarations:
            return node

        exact = _INSTALL_VARIABLE_PATTERN.fullmatch(node)
        if exact and exact.group(1) in resolved:
            return resolved[exact.group(1)]

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in declarations:
                return match.group(0)
            if key not in resolved:
                raise InstallError(
                    f"Blueprint install variable {key!r} is referenced but has no value"
                )
            value = resolved[key]
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            return str(value)

        return _INSTALL_VARIABLE_PATTERN.sub(substitute, node)

    materialized = {
        key: resolved[key]
        for key, declaration in declarations.items()
        if bool(declaration.get("materialize")) and key in resolved
    }
    return replace(payload), materialized


def _effective_install_todos(workspace: Workspace, todos: list[InstallTodo]) -> list[InstallTodo]:
    from packages.core.services.workspace_integration_scope import (
        disabled_workspace_providers,
        integration_provider_for_requirement,
    )

    disabled = disabled_workspace_providers(workspace.operating_model)
    return [
        todo for todo in todos
        if integration_provider_for_requirement({"kind": todo.kind, "payload": todo.payload}) not in disabled
    ]


def _persist_install_todos(
    workspace: Workspace,
    todos: list[InstallTodo],
    live_requirements: list[InstallTodo],
) -> None:
    """Store install results separately from the durable live contract."""

    def serialize(todo: InstallTodo) -> dict[str, Any]:
        return {
            "kind": (
                todo.kind.value
                if isinstance(todo.kind, BlueprintInstallTodoKind)
                else str(todo.kind)
            ),
            "detail": todo.detail,
            "payload": dict(todo.payload or {}),
            "blocking": bool(todo.blocking),
        }

    def identity(todo: InstallTodo) -> str:
        serialized = serialize(todo)
        kind = serialized["kind"]
        payload = serialized["payload"]
        if kind == BlueprintInstallTodoKind.POST_INSTALL_CHECK.value:
            payload = {"check": payload.get("check")}
        elif kind in {
            BlueprintInstallTodoKind.MCP_SERVER.value,
            BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
        }:
            kind = "mcp_binding"
            payload = {
                "server_slug": payload.get("server_slug"),
                "agent_slug": payload.get("agent_slug"),
            }
        elif kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value:
            payload = {"provider": payload.get("provider")}
        elif kind == BlueprintInstallTodoKind.MISSING_SKILL.value:
            payload = {
                "skill_slug": payload.get("skill_slug"),
                "marketplace_skill_id": payload.get("marketplace_skill_id"),
                "marketplace_source": payload.get("marketplace_source"),
                "skill_component_key": payload.get("skill_component_key"),
                "agent_slug": payload.get("agent_slug"),
                "installed_agent_id": payload.get("installed_agent_id"),
                "agent_component_key": payload.get("agent_component_key"),
            }
        return f"{kind}:{json.dumps(payload, sort_keys=True, default=str)}"

    settings = dict(workspace.settings or {})
    blueprint = dict(settings.get("_blueprint") or {})
    effective_todos = _effective_install_todos(workspace, todos)
    blueprint["install_todos"] = [serialize(todo) for todo in effective_todos]
    durable_requirements: list[dict[str, Any]] = []
    seen_requirements: set[str] = set()
    for requirement in [*live_requirements, *todos]:
        key = identity(requirement)
        if key in seen_requirements:
            continue
        seen_requirements.add(key)
        durable_requirements.append(serialize(requirement))
    blueprint["live_setup_requirements"] = durable_requirements
    blueprint["blocking_todo_count"] = sum(
        1 for todo in effective_todos if todo.blocking
    )
    settings["_blueprint"] = blueprint
    workspace.settings = settings


async def _bind_blueprint_channel_configs(
    db: AsyncSession,
    *,
    workspace: Workspace,
    channel_requirements: list[dict[str, Any]],
    selected_channel_config_ids: dict[str, str],
    user_id: str | None,
) -> list[dict[str, Any]]:
    """Bind selected accounts and return only changed routing snapshots for undo.

    Account credentials and message history are never part of these snapshots.
    """

    if not selected_channel_config_ids:
        return []
    if not user_id:
        raise InstallError("channel account selection requires an installing user")

    from packages.core.blueprints.setup_preflight import (
        BlueprintSetupPreflightError,
        BlueprintSetupPreflightFactory,
        blueprint_channel_requirement_key,
    )
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.services.channel_bindings import (
        preserve_channel_binding_route_order,
        snapshot_channel_binding,
    )

    try:
        selected_channel_config_ids = await BlueprintSetupPreflightFactory.resolve_channel_selections(
            db, channels=channel_requirements, entity_id=workspace.entity_id,
            user_id=user_id, selected=selected_channel_config_ids,
            workspace_id=workspace.id,
        )
    except BlueprintSetupPreflightError as exc:
        raise InstallError(str(exc)) from exc
    changes: list[dict[str, Any]] = []
    for index, requirement in enumerate(channel_requirements):
        if not isinstance(requirement, dict):
            continue
        requirement_key = blueprint_channel_requirement_key(index, requirement)
        channel_config_id = str(
            selected_channel_config_ids.get(requirement_key) or ""
        ).strip()
        if not channel_config_id:
            continue
        channel_type = str(requirement.get("channel_type") or "").strip()
        channel_config = await db.get(ChannelConfig, channel_config_id)
        if channel_config is None:
            raise InstallError(
                f"selected {channel_type!r} channel account is unavailable"
            )
        existing_binding = (await db.execute(
            select(Channel).where(
                Channel.entity_id == workspace.entity_id,
                Channel.workspace_id == workspace.id,
                Channel.config["channel_config_id"].astext == channel_config.id,
            ).limit(1).with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
        before = (
            snapshot_channel_binding(existing_binding)
            if existing_binding is not None else None
        )

        linked_service_key = str(
            requirement.get("linked_service_key") or ""
        ).strip()
        subscription = None
        if linked_service_key:
            subscription = (await db.execute(
                select(AgentSubscription).where(
                    AgentSubscription.entity_id == workspace.entity_id,
                    AgentSubscription.workspace_id == workspace.id,
                    AgentSubscription.service_key == linked_service_key,
                    AgentSubscription.status == "active",
                )
            )).scalar_one_or_none()
            if subscription is None:
                raise InstallError(
                    f"channel requires missing service {linked_service_key!r}"
                )
        else:
            subscriptions = (await db.execute(
                select(AgentSubscription).where(
                    AgentSubscription.entity_id == workspace.entity_id,
                    AgentSubscription.workspace_id == workspace.id,
                    AgentSubscription.status == "active",
                ).order_by(AgentSubscription.created_at, AgentSubscription.id).limit(2)
            )).scalars().all()
            # Older exports omitted the service mapping. Only a unique
            # deployment can recover it; otherwise retain the channel todo.
            if len(subscriptions) != 1:
                continue
            subscription = subscriptions[0]
            linked_service_key = subscription.service_key or ""

        if (
            channel_type == "slack"
            or existing_binding is None
            or existing_binding.status != "active"
        ):
            conflicting_binding = (await db.execute(
                select(Channel.id).where(
                    Channel.entity_id == workspace.entity_id,
                    Channel.type == channel_type,
                    Channel.status == "active",
                    Channel.config["channel_config_id"].astext
                    == channel_config.id,
                    *(
                        [Channel.id != existing_binding.id]
                        if existing_binding is not None
                        else []
                    ),
                ).limit(1)
            )).scalar_one_or_none()
            if conflicting_binding is not None:
                raise InstallError(
                    f"selected {channel_type} account already has an active Workspace binding"
                )

        binding_config = {
            "channel_config_id": channel_config.id,
            "blueprint_requirement_key": requirement_key,
            "role": str(
                requirement.get("role") or "primary_external"
            ).strip(),
            "purpose": str(requirement.get("purpose") or "").strip(),
            "linked_service_key": linked_service_key,
        }
        if existing_binding is not None:
            target_agent_id = (
                subscription.agent_id if subscription is not None else None
            )
            target_subscription_id = (
                subscription.id if subscription is not None else None
            )
            if channel_type == "twilio_voice" and (
                existing_binding.agent_id != target_agent_id
                or existing_binding.agent_subscription_id
                != target_subscription_id
                or existing_binding.workspace_id != workspace.id
                or existing_binding.status != "active"
            ):
                from packages.core.services.voice.call_sessions import (
                    cancel_unconnected_call_sessions_for_binding,
                )

                await cancel_unconnected_call_sessions_for_binding(
                    db,
                    channel_config_id=channel_config.id,
                    channel_binding_id=existing_binding.id,
                    reason=(
                        "Twilio Voice Blueprint binding changed before the call "
                        "connected."
                    ),
                )
            existing_binding.name = str(
                requirement.get("label")
                or existing_binding.name
                or channel_config.name
                or channel_type
            )
            existing_binding.agent_id = target_agent_id
            existing_binding.agent_subscription_id = target_subscription_id
            # Blueprint owns routing fields, not per-Workspace runtime options
            # such as the operator's language preference.
            existing_binding.config = {
                **(existing_binding.config or {}),
                **binding_config,
            }
            existing_binding.status = "active"
            existing_binding.user_id = channel_config.owner_user_id
        else:
            existing_binding = Channel(
                id=generate_ulid(),
                entity_id=workspace.entity_id,
                user_id=channel_config.owner_user_id,
                workspace_id=workspace.id,
                type=channel_type,
                name=str(
                    requirement.get("label")
                    or channel_config.name
                    or channel_type
                ),
                agent_id=subscription.agent_id if subscription is not None else None,
                agent_subscription_id=(
                    subscription.id if subscription is not None else None
                ),
                config=binding_config,
                status="active",
            )
            db.add(existing_binding)
        after = snapshot_channel_binding(existing_binding)
        if before != after:
            if before is not None:
                preserve_channel_binding_route_order(existing_binding, before["updated_at"])
            changes.append({
                "id": existing_binding.id,
                "kind": "channel",
                "name": existing_binding.name,
                "before": before,
                "after": after,
            })
    await db.flush()
    return changes


def _live_channel_requirement(
    index: int, channel: dict[str, Any], configured_channels: list[dict[str, Any]],
) -> InstallTodo:
    from packages.core.blueprints.setup_preflight import blueprint_channel_requirement_key
    from packages.core.services.workspace_readiness import matching_blueprint_channels

    payload = {
        **channel,
        "blueprint_requirement_key": blueprint_channel_requirement_key(index, channel),
    }
    matches = matching_blueprint_channels(configured_channels, payload)
    if len(matches) == 1:
        for key in ("channel_config_id", "channel_binding_id"):
            if matches[0].get(key):
                payload[key] = matches[0][key]
    return InstallTodo(
        kind=BlueprintInstallTodoKind.CHANNEL.value,
        detail=f"Pair a {channel.get('channel_type')} channel"
        + (f" for {channel.get('purpose')}" if channel.get("purpose") else ""),
        payload=payload,
        blocking=bool(channel.get("required", True)),
    )


async def sync_workspace_live_setup_requirements(
    db: AsyncSession,
    *,
    workspace: Workspace,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild a trusted live setup contract from a reviewed payload.

    This is the explicit recovery path for installs created before
    ``live_setup_requirements`` existed. It reads the already-installed local
    identities; it does not reinstall components or copy runtime history.
    """

    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
    )

    locked_workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace.id,
        entity_id=workspace.entity_id,
    )
    if locked_workspace is None or locked_workspace.deleted_at is not None:
        raise ValueError("Workspace not found")
    workspace = locked_workspace

    contract = payload.get("contract") or {}
    embedded = payload.get("embedded") or {}
    recipe = payload.get("recipe") or {}
    policy = payload.get("policy") or {}
    requirements: list[InstallTodo] = []

    subscriptions = list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace.id,
            AgentSubscription.entity_id == workspace.entity_id,
        )
    )).scalars().all())
    deployed_agent_ids = {str(row.agent_id) for row in subscriptions}
    agents = list((await db.execute(
        select(Agent).where(
            Agent.entity_id == workspace.entity_id,
            Agent.deleted_at.is_(None),
            or_(
                Agent.workspace_id == workspace.id,
                Agent.id.in_(deployed_agent_ids),
            ),
        )
    )).scalars().all())

    def installed_agent(spec: dict[str, Any]) -> Agent | None:
        component_key = str(
            spec.get("component_id") or spec.get("id") or spec.get("slug") or ""
        ).strip()
        slug = str(spec.get("slug") or "").strip()
        exact = [
            row for row in agents
            if row.workspace_id == workspace.id
            and str(
                (row.config or {}).get("source_blueprint_component_key") or ""
            ).strip() == component_key
        ]
        if len(exact) == 1:
            return exact[0]
        matches = [
            row for row in agents
            if row.workspace_id == workspace.id
            and str(row.slug or "").strip() == slug
        ]
        return matches[0] if len(matches) == 1 else None

    required_skills: dict[str, list[dict[str, Any]]] = {}
    for item in (contract.get("requires") or {}).get("skills") or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if slug:
            required_skills.setdefault(slug, []).append(item)
    embedded_skill_component_keys = {
        str(item.get("slug") or "").strip(): str(
            item.get("component_id")
            or item.get("id")
            or item.get("slug")
            or ""
        ).strip()
        for item in embedded.get("skills") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }

    required_mcp = {
        str(item.get("slug") or "").strip(): bool(item.get("required", True))
        for item in (contract.get("requires") or {}).get("mcp_servers") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }
    mcp_fields = {
        str(item.get("slug") or "").strip(): [
            str(field).strip()
            for field in item.get("config_fields_to_set") or []
            if str(field).strip()
        ]
        for item in (contract.get("requires") or {}).get("mcp_servers") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }

    from packages.core.services.provider_keys import canonical_provider_key

    for provider_slug, blocking in required_mcp.items():
        provider = canonical_provider_key(provider_slug)
        if not is_user_facing_integration_provider(provider):
            continue
        requirements.append(InstallTodo(
            kind=BlueprintInstallTodoKind.MISSING_INTEGRATION.value,
            detail=f"Connect the required {provider!r} integration.",
            payload={
                "provider": provider,
                "config_fields_to_set": mcp_fields.get(provider_slug, []),
            },
            blocking=blocking,
        ))

    agent_ids = {str(row.id) for row in agents}
    skill_rows = list((await db.execute(
        select(AgentSkillBinding, Skill).join(
            Skill,
            Skill.id == AgentSkillBinding.skill_id,
        ).where(
            AgentSkillBinding.agent_id.in_(agent_ids),
            AgentSkillBinding.status == "active",
            Skill.status == "active",
            or_(Skill.entity_id == workspace.entity_id,
                and_(Skill.entity_id.is_(None), Skill.is_public.is_(True))),
        )
    )).all()) if agent_ids else []
    skills_by_agent: dict[str, list[Skill]] = {}
    for binding, skill in skill_rows:
        skills_by_agent.setdefault(str(binding.agent_id), []).append(skill)

    for agent_spec in embedded.get("agents") or []:
        if not isinstance(agent_spec, dict):
            continue
        agent_slug = str(agent_spec.get("slug") or "").strip()
        agent_component_key = str(
            agent_spec.get("component_id")
            or agent_spec.get("id")
            or agent_slug
        ).strip()
        agent = installed_agent(agent_spec)
        installed_agent_id = str(agent.id) if agent is not None else None
        for binding in agent_spec.get("mcp_bindings") or []:
            if not isinstance(binding, dict):
                continue
            server_slug = str(binding.get("server_slug") or "").strip()
            if not server_slug:
                continue
            if not is_user_facing_integration_provider(server_slug):
                continue
            requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
                detail=(
                    f"Restore the {server_slug!r} MCP binding for agent "
                    f"{agent_slug!r}."
                ),
                payload={
                    "server_slug": server_slug,
                    "agent_slug": agent_slug,
                    "installed_agent_id": installed_agent_id,
                    "agent_component_key": agent_component_key,
                    "allowed_tools": binding.get("allowed_tools"),
                    "required_config_fields": sorted({
                        str(field).strip()
                        for field in binding.get("config_override_allowlist") or []
                        if str(field).strip()
                    }),
                },
                blocking=required_mcp.get(server_slug, True),
            ))

        exact_refs = [
            ref for ref in agent_spec.get("skill_binding_refs") or []
            if isinstance(ref, dict)
            and str(ref.get("marketplace_id") or "").strip()
        ]
        exact_slugs = {
            str(ref.get("slug") or "").strip()
            for ref in exact_refs
            if str(ref.get("slug") or "").strip()
        }
        binding_refs: list[Any] = [*exact_refs]
        binding_refs.extend(
            ref for ref in agent_spec.get("skill_bindings") or []
            if str(ref or "").strip() not in exact_slugs
        )
        for binding_ref in binding_refs:
            exact_ref = binding_ref if isinstance(binding_ref, dict) else {}
            skill_slug = str(
                exact_ref.get("slug") if exact_ref else binding_ref
            ).strip()
            candidates = required_skills.get(skill_slug) or []
            requirement = candidates[0] if len(candidates) == 1 else {}
            marketplace_skill_id = str(
                exact_ref.get("marketplace_id")
                or requirement.get("marketplace_id")
                or ""
            ).strip()
            marketplace_source = str(
                exact_ref.get("marketplace_source")
                or requirement.get("marketplace_source") or "platform"
            ).strip()
            installed_skill_id: str | None = None
            bound_skills = skills_by_agent.get(installed_agent_id or "", [])
            if marketplace_skill_id:
                from packages.core.services.marketplace_skill_service import resolve_bound_blueprint_skill

                installed_skill_id = await resolve_bound_blueprint_skill(
                    db, entity_id=workspace.entity_id, skills=bound_skills,
                    marketplace_id=marketplace_skill_id,
                    marketplace_source=marketplace_source,
                )
            elif not candidates or len(candidates) == 1:
                matches = {skill.id for skill in bound_skills
                           if str(skill.slug or "").strip() == skill_slug}
                installed_skill_id = next(iter(matches)) if len(matches) == 1 else None
            requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_SKILL.value,
                detail=f"Restore skill {skill_slug!r} for agent {agent_slug!r}.",
                payload={
                    "skill_slug": skill_slug,
                    "marketplace_skill_id": marketplace_skill_id or None,
                    **({"marketplace_source": marketplace_source} if marketplace_skill_id else {}),
                    "skill_component_key": embedded_skill_component_keys.get(
                        skill_slug
                    ),
                    "installed_skill_id": installed_skill_id,
                    "agent_slug": agent_slug,
                    "installed_agent_id": installed_agent_id,
                    "agent_component_key": agent_component_key,
                },
                blocking=True,
            ))

    subscriptions_by_service = {
        str(row.service_key or "").strip(): row
        for row in subscriptions
        if str(row.service_key or "").strip()
    }
    for subscription in recipe.get("subscriptions") or []:
        if not isinstance(subscription, dict):
            continue
        service_key = str(subscription.get("service_key") or "").strip()
        installed = subscriptions_by_service.get(service_key)
        requirements.append(InstallTodo(
            kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
            detail=f"Restore the installed Agent for service {service_key!r}.",
            payload={
                "service_key": service_key,
                "agent_slug": subscription.get("agent_slug"),
                "marketplace_agent_id": subscription.get("marketplace_agent_id"),
                "installed_agent_id": (
                    str(installed.agent_id) if installed is not None else None
                ),
            },
            blocking=True,
        ))

    from packages.core.services.workspace_readiness import list_configured_workspace_channels

    configured_channels = await list_configured_workspace_channels(db, workspace)
    for index, channel in enumerate(contract.get("channels") or []):
        if isinstance(channel, dict):
            requirements.append(_live_channel_requirement(index, channel, configured_channels))
    for session in contract.get("sessions") or []:
        if isinstance(session, dict):
            requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.BROWSER_SESSION.value,
                detail=f"Capture a {session.get('provider')} browser session.",
                payload=dict(session),
                blocking=bool(session.get("required", True)),
            ))
    for check in policy.get("post_install_checks") or []:
        if isinstance(check, dict) and str(check.get("kind") or "").strip() in {
            "session_alive",
            "agent_callable",
            "cron_scheduled",
            "workflow_present",
            "workflow_dryrun",
        }:
            requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    "Required Blueprint post-install check is no longer ready: "
                    f"{check.get('kind')}."
                ),
                payload={"check": dict(check)},
                blocking=True,
            ))

    blueprint = dict((workspace.settings or {}).get("_blueprint") or {})
    # Accept current identity-pinned receipts as well as unchanged legacy
    # declarations, but never carry forward a superseded channel requirement.
    current_channel_payloads = [
        requirement.payload for requirement in requirements
        if requirement.kind == BlueprintInstallTodoKind.CHANNEL.value
    ] + list(contract.get("channels") or [])
    existing_todos = [
        InstallTodo(
            kind=str(item.get("kind") or BlueprintInstallTodoKind.NOTE.value),
            detail=str(item.get("detail") or "Blueprint setup is incomplete."),
            payload=(
                dict(item.get("payload"))
                if isinstance(item.get("payload"), dict)
                else {}
            ),
            blocking=bool(item.get("blocking", True)),
        )
        for item in blueprint.get("install_todos") or []
        if isinstance(item, dict)
        # Channel declarations are reconciled by this reviewed payload. Keep
        # unchanged pending todos, but do not revive superseded requirements.
        # Other install failures are outside that reconciliation boundary.
        and (
            item.get("kind") != BlueprintInstallTodoKind.CHANNEL.value
            or item.get("payload") in current_channel_payloads
        )
    ]
    _persist_install_todos(workspace, existing_todos, requirements)
    await db.flush()
    return list(
        ((workspace.settings or {}).get("_blueprint") or {}).get(
            "live_setup_requirements"
        )
        or []
    )


# ── Public API ────────────────────────────────────────────────────────

def _declare_subscribed_services(
    operating_model: dict[str, Any],
    recipe: Mapping[str, Any],
    embedded: Mapping[str, Any],
) -> None:
    """Declare every subscribed service the payload's service list omitted.

    Once a Workspace declares services, each Agent mapping must name one of
    them; otherwise every later Workspace operation inherits validation errors
    (R2-4: a merged payload subscribed ``product_video.*`` roles but declared
    only ``video_studio.*``). Installed state must validate, so an incomplete
    payload is completed here from its own subscription and Agent data.
    A payload declaring no services keeps that shape: nothing is checked
    against an empty list, and synthesizing one would change its meaning.
    """
    services = operating_model.get("services")
    if not isinstance(services, list) or not services:
        return
    declared = {
        str(row.get("key") or row.get("service_key") or "").strip()
        for row in services if isinstance(row, dict)
    }
    agent_names = {
        agent.get("slug"): agent.get("name")
        for agent in embedded.get("agents") or [] if isinstance(agent, dict)
    }
    added = []
    for sub in recipe.get("subscriptions") or []:
        key = str(sub.get("service_key") or "").strip() if isinstance(sub, dict) else ""
        if not key or key in declared:
            continue
        config = sub.get("config") if isinstance(sub.get("config"), dict) else {}
        name = config.get("service_name") or agent_names.get(sub.get("agent_slug")) or key
        operating_model["services"] = services = [*services, {"key": key, "name": str(name)}]
        declared.add(key)
        added.append(key)
    if added:
        logger.info("blueprint subscriptions referenced undeclared services; declared %s", added)


async def install_blueprint(
    db: AsyncSession,
    *,
    entity_id: str,
    payload: dict[str, Any],
    mode: InstallMode = InstallMode.SIMULATE,
    workspace_name: Optional[str] = None,
    user_id: Optional[str] = None,
    blueprint_id: Optional[str] = None,
    blueprint_slug: Optional[str] = None,
    blueprint_version: Optional[str] = None,
    create_missing_agents: bool = False,
    agent_ids_by_service_key: Optional[dict[str, str]] = None,
    governance_preset: str = "automatic",
    variable_values: Optional[dict[str, Any]] = None,
    channel_config_ids: Optional[dict[str, str]] = None,
    install_author_hard_blocks: bool = False,
) -> InstallResult:
    """Materialise a blueprint payload as a new workspace. Caller commits.

    ``workspace_name`` overrides the blueprint's title for the workspace
    name — handy when the operator wants their own label.
    """
    try:
        validate_payload(payload)
        # Normalise to v1.1 shape — v1.0 payloads get auto-migrated here
        # so the rest of this function reads a single canonical layout.
        source_payload = migrate_payload(payload)
    except PayloadError as exc:
        raise InstallError(f"invalid blueprint payload: {exc}") from exc

    _assert_runtime_compatible(source_payload)
    payload, personalization = resolve_install_variables(
        source_payload,
        variable_values,
    )
    try:
        validate_payload(payload)
    except PayloadError as exc:
        raise InstallError(f"resolved blueprint payload is invalid: {exc}") from exc

    # Runtime contracts are part of the portable Blueprint contract. Validate
    # every translated Workflow before locking or materializing the Workspace;
    # otherwise a malformed schema can leave partial install state in the
    # caller's transaction before the Workflow phase eventually rejects it.
    for workflow in (payload.get("recipe") or {}).get("workflows") or []:
        if not isinstance(workflow, dict):
            raise InstallError("workflow definition must be an object")
        _blueprint_workflow_definition_values(workflow)

    manifest = payload["manifest"]
    contract = payload["contract"]
    recipe = payload["recipe"]
    selected_agents = agent_ids_by_service_key or {}
    declared_service_keys = {
        sub["service_key"] for sub in recipe.get("subscriptions") or []
    }
    if any(
        not isinstance(key, str) or key not in declared_service_keys
        or not isinstance(agent_id, str) or not agent_id.strip()
        for key, agent_id in selected_agents.items()
    ):
        raise InstallError("Agent selections must name a Blueprint service and an existing Agent id")
    if selected_agents and not user_id:
        raise InstallError("Selecting an existing Agent requires an installing user")
    await lock_reusable_resource_lifecycle(db, entity_id=entity_id)
    policy = payload["policy"]

    # One materialized governance projection owns source rules, embedded-tool
    # preview, and the persisted policy. Keep the delivered payload unchanged
    # for provenance, personalization fingerprints, and upgrade comparisons.
    from packages.core.governance.defaults import normalize_default_workspace_governance
    from packages.core.governance.presets import PRESETS, apply_preset

    if governance_preset not in PRESETS:
        raise InstallError(
            f"unknown governance_preset {governance_preset!r}; "
            f"valid: {sorted(PRESETS)}"
        )
    governance_section = policy.get("governance") or {}
    try:
        from packages.core.governance.approval_projection import import_blueprint_governance
        base_policy, imported_rules = import_blueprint_governance(
            governance_section, (recipe.get("operating_model") or {}).get("rules") or [],
            preserve_hard_blocks=install_author_hard_blocks,
        )
        operating_rules, final_policy, demoted_hard_blocks = normalize_default_workspace_governance(
            imported_rules, base_policy,
            preserve_hard_blocks=install_author_hard_blocks,
        )
        # Template consent is nonportable; the installer's execution choice is
        # local operator configuration and must survive template normalization.
        final_policy = apply_preset(final_policy, governance_preset)
    except (ValueError, TypeError) as exc:
        raise InstallError(f"governance_policy malformed: {exc}") from exc

    # Simulation never creates reusable approval authority.
    simulation_task_grant_added = False

    if channel_config_ids is not None and (
        mode != InstallMode.SIMULATE or channel_config_ids
    ):
        from packages.core.blueprints.setup_preflight import (
            BlueprintSetupPreflightError,
            BlueprintSetupPreflightFactory,
        )

        try:
            channel_config_ids = await BlueprintSetupPreflightFactory.resolve_channel_selections(
                db, channels=list(contract.get("channels") or []),
                entity_id=entity_id, user_id=user_id, selected=channel_config_ids,
            )
        except BlueprintSetupPreflightError as exc:
            raise InstallError(str(exc)) from exc

    # operating_model absorbs the workspace shell fields (kind / context /
    # primary_work / settings) during migration. Extract them back out
    # before writing the JSONB column so the column stays clean.
    om_full = dict(recipe.get("operating_model") or {})
    if "rules" in om_full:
        om_full["rules"] = operating_rules
    ws_kind = manifest.get("kind") or om_full.pop("kind", None) or ""
    operating_context = om_full.pop("context", None) or ""
    primary_work = om_full.pop("primary_work", None) or ""
    heartbeat_enabled = bool(om_full.pop("heartbeat_enabled", False))
    heartbeat_cadence = om_full.pop("heartbeat_cadence", None)
    ws_settings_seed = dict(om_full.pop("settings", None) or {})
    runtime_contract_fingerprints: dict[str, str] = {}
    blocking_setup = ws_settings_seed.get("blocking_setup")
    if isinstance(blocking_setup, dict):
        from packages.core.services.blueprint_startup_service import (
            blueprint_startup_contract_fingerprint,
        )

        runtime_contract_fingerprints["blocking_setup"] = (
            blueprint_startup_contract_fingerprint(blocking_setup)
        )

    # Strategist template (recipe.strategist) goes into
    # operating_model.strategist. Split the nested ``cadence`` so legacy
    # readers (which expect a string at operating_model.strategist.cadence)
    # keep working — the structured trigger_conditions live as a peer.
    strategist_cfg = recipe.get("strategist")
    if isinstance(strategist_cfg, dict) and strategist_cfg:
        merged = dict(om_full.get("strategist") or {})
        cadence_obj = strategist_cfg.get("cadence")
        if isinstance(cadence_obj, dict):
            if cadence_obj.get("schedule"):
                merged["cadence"] = cadence_obj["schedule"]
            tc = cadence_obj.get("trigger_conditions")
            if tc is not None:
                merged["trigger_conditions"] = tc
        elif isinstance(cadence_obj, str):
            merged["cadence"] = cadence_obj
        for key in (
            "business_model", "proposal_shape", "priors",
            "evaluation_rubric", "do_not_propose", "voice",
            "system_prompt_override", "use_goals",
        ):
            if key in strategist_cfg:
                merged[key] = strategist_cfg[key]
        om_full["strategist"] = merged

    _declare_subscribed_services(om_full, recipe, payload.get("embedded") or {})
    workspace_operating_model = om_full  # remaining keys (services, rules, strategist, ...)
    # Prompts are portable workspace guidance, not executable task instances.
    # Keep them under the operating model so export/install has one durable
    # owner instead of accepting and silently dropping recipe.prompts.
    if recipe.get("prompts"):
        workspace_operating_model["blueprint_prompts"] = [dict(prompt) for prompt in recipe["prompts"]]
    governance_rules = [
        dict(rule) for rule in recipe.get("escalation_rules") or []
        if isinstance(rule, dict) and not (
            str(rule.get("sla_policy_key") or rule.get("sla_key") or "").strip()
        )
    ]
    if governance_rules:
        workspace_operating_model["blueprint_governance_rules"] = governance_rules

    name = workspace_name or manifest.get("title") or "Untitled workspace"
    if mode == InstallMode.SIMULATE:
        # Distinguish in the workspace list — but we keep the underlying
        # `kind` so promote() can restore.
        name = f"[SIM] {name}"

    workspace = await create_workspace(
        db, entity_id,
        name=name,
        description=manifest.get("description") or "",
        kind=ws_kind,
        operating_context=operating_context,
        primary_work=primary_work,
        operating_model=workspace_operating_model,
        settings=ws_settings_seed,
        match_business_ledgers=False,
    )

    # Blueprint installs materialize only declared portable capabilities.
    # Business matching is reserved for ordinary Workspace creation.
    workspace.heartbeat_enabled = heartbeat_enabled
    workspace.heartbeat_cadence = heartbeat_cadence
    settings = settings_with_default_workspace_access(workspace.settings)
    if user_id:
        settings.setdefault("created_by_user_id", user_id)
    if personalization:
        settings["blueprint_personalization"] = dict(personalization)
    if mode == InstallMode.SIMULATE:
        settings["sandbox"] = True
        # A simulation must actually run, not merely rename the Workspace.
        # Auto-approve Strategist *task proposals* so the first dispatched
        # review immediately starts Planner/Executor in Workspace simulation
        # mode. Runtime action governance still applies, and simulation plans route external
        # side effects to their simulated adapters.
        strategist_settings = dict(settings.get("strategist") or {})
        if simulation_task_grant_added:
            strategist_settings["proposal_task_approval_source"] = "blueprint_simulation"
        else:
            # A grant retained from the installed policy is not simulation-owned.
            strategist_settings.pop("proposal_task_approval_source", None)
        settings["strategist"] = strategist_settings
        settings["simulation_experience"] = resolve_simulation_experience(payload)
    else:
        settings.pop("sandbox", None)
        settings.pop("simulation_experience", None)
    settings["_blueprint"] = {
        "blueprint_id": blueprint_id,
        "blueprint_slug": blueprint_slug,
        # Raw JSON is portable configuration but has no durable release
        # provenance.  Never let its manifest slug become an upgrade source.
        "source": "marketplace" if blueprint_id else "raw_payload",
        "title": manifest.get("title"),
        "usage_guide": manifest.get("usage_guide"),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        # What was actually copied, so a later blueprint fix can be detected.
        # manifest.blueprint_version is the payload *format* version and does
        # not move when the content does — it read "1.1" across the whole
        # rewrite that left one workspace running a 636-character skill while
        # the blueprint had already been corrected to 4664.
        # Identity is the blueprint id, always — a built-in carries the
        # marketplace id ``builtin:<slug>``, so nothing has to fall back to
        # matching on a slug.
        BLUEPRINT_VERSION_KEY: blueprint_version,
        CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(source_payload),
        SECTION_FINGERPRINTS_KEY: blueprint_section_fingerprints(source_payload),
        UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
            blueprint_upgrade_unsupported_fingerprint(source_payload)
        ),
        MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: (
            blueprint_upgrade_unsupported_fingerprint(payload)
        ),
        "install_mode": mode.value,
        "original_kind": ws_kind,
        "simulation_auto_run": mode == InstallMode.SIMULATE,
        # Preserve only the names of payload-declared settings. The values are
        # materialized on Workspace.settings and never copied into provenance.
        "portable_setting_keys": sorted(ws_settings_seed),
        "runtime_contract_fingerprints": runtime_contract_fingerprints,
        # Creator declarations are safe portable schema. Installer values live
        # separately on Workspace.settings and are never copied into provenance.
        "variable_declarations": [
            dict(item)
            for item in (source_payload.get("contract") or {}).get("variables") or []
            if isinstance(item, dict)
        ],
        # Persist requirement lists so promote() can re-check them.
        # NOTE: promote.py still reads these top-level keys for back-compat.
        "channel_requirements": list(contract.get("channels") or []),
        "session_requirements": list(contract.get("sessions") or []),
    }
    # Name the exact release installed, when the registry holds it.
    await sync_installed_package(db, settings["_blueprint"])
    workspace.settings = settings
    await db.flush()
    await ensure_workspace_owner_membership(
        db,
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=user_id,
        added_by=user_id,
    )

    result = InstallResult(
        workspace_id=workspace.id,
        mode=mode,
        blueprint_id=blueprint_id,
        blueprint_slug=blueprint_slug,
    )
    live_setup_requirements: list[InstallTodo] = []
    if blueprint_id:
        await record_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_FROM,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace.id,
            local_resource_type=RESOURCE_WORKSPACE,
            local_resource_id=workspace.id,
            marketplace_version=(
                str(blueprint_version) if blueprint_version is not None else None
            ),
            linked_by=user_id,
            metadata={"source_slug": blueprint_slug},
        )

    # ── Final governance policy preview ──
    # Compute the post-preset policy BEFORE installing embedded agents so
    # we can reject blueprints that try to bind agents to actions the
    # operator's chosen preset would never allow. The actual policy row
    # is written later in this function (one source of truth, same
    # values).
    final_policy_preview = final_policy

    # ── Embedded skills ──
    # Skills first because embedded agents may bind to them.
    embedded = payload["embedded"]
    skill_id_by_slug: dict[str, str] = {}
    for sk in embedded.get("skills") or []:
        sk_id = await _install_embedded_skill(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            source_blueprint_id=blueprint_id,
            owner_user_id=user_id,
            sk=sk,
        )
        if sk_id and sk.get("slug"):
            skill_id_by_slug[sk["slug"]] = sk_id

    # External skill requirements can refer to platform built-ins such as the
    # Chrome runtime skill. Seed those rows before resolving agent bindings so
    # a clean deployment does not turn a bundled capability into a blocking
    # ``missing_skill`` install todo merely because no earlier request happened
    # to initialize the built-in catalog.
    if (contract.get("requires") or {}).get("skills"):
        from packages.core.services.builtin_skill_loader import seed_builtin_skills

        await seed_builtin_skills(db)

    # Keep the persisted binding catalog aligned with the live runtime before
    # validating embedded agent tool bindings. Dev and upgraded deployments can
    # legitimately have a stale ToolDefinition snapshot even though the tool is
    # already registered and executable.
    from packages.core.services.agent_service import ensure_runtime_tool_definitions

    await ensure_runtime_tool_definitions(db)

    required_mcp_by_slug = {
        str(item.get("slug") or "").strip(): bool(item.get("required", True))
        for item in (contract.get("requires") or {}).get("mcp_servers") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }
    from packages.core.services.provider_keys import canonical_provider_key

    required_mcp_by_provider = {
        canonical_provider_key(slug): required
        for slug, required in required_mcp_by_slug.items()
    }
    mcp_setup_fields_by_provider = {
        canonical_provider_key(str(item.get("slug") or "").strip()): [
            str(field).strip()
            for field in item.get("config_fields_to_set") or []
            if str(field).strip()
        ]
        for item in (contract.get("requires") or {}).get("mcp_servers") or []
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }

    # ── Embedded agents (with tool / MCP / skill bindings + starter_memory) ──
    agent_id_by_slug: dict[str, str] = {}
    required_skill_refs: dict[str, list[dict[str, Any]]] = {}
    for item in (contract.get("requires") or {}).get("skills") or []:
        if not isinstance(item, dict):
            continue
        requirement_slug = str(item.get("slug") or "").strip()
        if requirement_slug:
            required_skill_refs.setdefault(requirement_slug, []).append(item)
    for a in embedded.get("agents") or []:
        agent_id = await _install_embedded_agent(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            source_blueprint_id=blueprint_id,
            owner_user_id=user_id,
            a=a,
            skill_id_by_slug=skill_id_by_slug,
            required_skill_refs=required_skill_refs,
            required_mcp_by_slug=required_mcp_by_slug,
            live_setup_requirements=live_setup_requirements,
            final_policy=final_policy_preview,
            todos=result.todos,
        )
        if agent_id and a.get("slug"):
            agent_id_by_slug[str(a["slug"])] = agent_id

    required_mcp_slugs = list(required_mcp_by_slug)
    if required_mcp_slugs:
        from packages.core.services.integration_resolution import (
            integration_provider_readiness,
        )

        readiness = await integration_provider_readiness(
            db,
            entity_id=entity_id,
            user_id=user_id,
            provider_keys=[
                slug for slug in required_mcp_slugs
                if is_user_facing_integration_provider(slug)
            ],
        )
        for state in readiness.values():
            provider = state.provider
            requirement = InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_INTEGRATION.value,
                detail=f"Connect the required {provider!r} integration.",
                payload={
                    "provider": provider,
                    "setup_kind": state.setup_kind,
                    "scope": state.scope,
                    "config_fields_to_set": mcp_setup_fields_by_provider.get(
                        provider,
                        [],
                    ),
                },
                blocking=required_mcp_by_provider.get(provider, True),
            )
            live_setup_requirements.append(requirement)
            if not state.ready:
                result.todos.append(InstallTodo(
                    kind=requirement.kind,
                    detail=state.reason,
                    payload=dict(requirement.payload),
                    blocking=requirement.blocking,
                ))

    # ── Knowledge packs ──
    for kp in embedded.get("knowledge_packs") or []:
        await _install_knowledge_pack(
            db, entity_id=entity_id, workspace_id=workspace.id, kp=kp,
            todos=result.todos,
        )

    # ── Subscriptions ──
    for sub in recipe.get("subscriptions") or []:
        sub_id, todo = await _install_subscription(
            db, entity_id=entity_id, workspace_id=workspace.id, sub=sub,
            create_missing=create_missing_agents, owner_user_id=user_id,
            embedded_agent_ids=agent_id_by_slug,
            selected_agent_id=selected_agents.get(sub["service_key"]),
        )
        if sub_id:
            result.subscription_ids.append(sub_id)
            installed_subscription = await db.get(AgentSubscription, sub_id)
            if installed_subscription is not None:
                live_setup_requirements.append(InstallTodo(
                    kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                    detail=(
                        f"Restore the installed Agent for service "
                        f"{installed_subscription.service_key!r}."
                    ),
                    payload={
                        "service_key": installed_subscription.service_key,
                        "agent_slug": sub.get("agent_slug"),
                        "installed_agent_id": installed_subscription.agent_id,
                    },
                    blocking=True,
                ))
        if todo:
            result.todos.append(todo)

    # ── Custom fields ──
    for cf in recipe.get("custom_fields") or []:
        cfd_id = await _install_custom_field(
            db, entity_id=entity_id, workspace_id=workspace.id, cf=cf,
        )
        result.custom_field_ids.append(cfd_id)

    # Workflows, Stats and Jobs are also materialized for an AI-created Draft.
    # Keep their native installation behavior in one primitive.
    workflow_source_id = str(
        blueprint_id
        or f"blueprint-payload:{blueprint_content_fingerprint(source_payload)}"
    )
    workflow_source_template_id = blueprint_workflow_installation_source_id(
        workflow_source_id,
        workspace.id,
    )
    workflow_source_version = str(
        blueprint_version or manifest.get("blueprint_version") or "1.0.0"
    )
    materialized_resources = await materialize_workspace_recipe_resources(
        db, entity_id=entity_id, workspace=workspace, recipe=recipe,
        user_id=user_id, mode=mode, source_template_id=workflow_source_template_id,
        source_version=workflow_source_version,
        binding_source_template_id=workflow_source_id,
        skill_id_by_component_key=skill_id_by_slug,
    )
    result.stat_ids.extend(materialized_resources["stat_ids"])
    result.workflow_ids.extend(materialized_resources["workflow_ids"].values())
    result.workflow_binding_ids.extend(materialized_resources["workflow_binding_ids"].values())
    result.scheduled_job_ids.extend(materialized_resources["scheduled_job_ids"])
    workflow_id_by_key = dict(materialized_resources["workflow_ids"])
    app_binding_id_by_key = dict(materialized_resources["workflow_binding_ids"])

    # ``active`` is the Strategist runtime state. A Blueprint that deploys
    # only explicitly enabled Workflows keeps those automations independent
    # while its Strategist starts paused. Simulation remains runnable so its
    # declared walkthrough can execute against sandbox adapters.
    if mode != InstallMode.SIMULATE and not heartbeat_enabled:
        workspace.status = "paused"

    # ── Goals (stats have been materialized above) ──
    for g in recipe.get("goals") or []:
        gid = await _install_goal(
            db, entity_id=entity_id, workspace_id=workspace.id, g=g,
            runtime_enabled=(workspace.status == "active" and bool(workspace.heartbeat_enabled)),
        )
        result.goal_ids.append(gid)

    for w in recipe.get("workflows") or []:
        workflow_id = workflow_id_by_key.get(str(w.get("slug") or "").strip())
        if workflow_id and blueprint_id:
            await record_marketplace_resource_link(
                db,
                entity_id=entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=blueprint_id,
                relationship=RELATIONSHIP_INSTALLED_COMPONENT,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace.id,
                local_resource_type=RESOURCE_WORKFLOW,
                local_resource_id=workflow_id,
                component_key=str(w.get("component_id") or w.get("id") or w.get("slug")),
                marketplace_version=workflow_source_version,
                linked_by=user_id,
                metadata={"source_slug": w.get("slug")},
            )

    if workspace_operating_model.get("app_view") is not None:
        from packages.core.blueprints.app_view import remap_app_view
        from packages.core.blueprints.exporter import _portable_agent_slug

        app_agents = (await db.execute(select(Agent).join(
            AgentSubscription, AgentSubscription.agent_id == Agent.id,
        ).where(AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.status == "active"))).scalars().all()
        try:
            workspace.operating_model = {
                **workspace.operating_model,
                "app_view": remap_app_view(
                    workspace_operating_model["app_view"], workflows=app_binding_id_by_key,
                    workflow_definitions=workflow_id_by_key,
                    stat_keys=set(materialized_resources["stat_ids_by_key"]),
                    agents={_portable_agent_slug(a): a.id for a in app_agents},
                    automations={j["job_id"]: installed_blueprint_job_id(j["job_id"], workspace.id)
                                 for j in recipe.get("scheduled_jobs") or []},
                ),
            }
        except ValueError as exc:
            raise InstallError(str(exc)) from exc

    # ── Governance policy ──
    # The post-preset policy was already computed at the top of this
    # function (so embedded agents could be validated against it). Reuse
    # those values here — there's only one source of truth.
    result.demoted_hard_blocks = demoted_hard_blocks

    # Always write a policy row so the audit chain shows the active
    # ruleset — even when the blueprint had no governance section but
    # the operator picked 'safe'.
    if (
        governance_section
        or governance_preset != "follow_default"
        or final_policy != base_policy
    ):
        await update_policy(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            policy=final_policy,
            changed_by=user_id,
            change_summary=(
                f"installed from blueprint "
                f"{blueprint_slug or blueprint_id or '<inline>'} "
                f"(preset={governance_preset})"
            ),
        )
        result.governance_applied = True

    # Stash the preset choice in the blueprint metadata so the
    # simulation report can later say "you ran on Safe".
    bp_meta = dict(workspace.settings.get("_blueprint") or {})
    bp_meta["governance_preset"] = governance_preset
    bp_meta["author_hard_blocks_installed"] = bool(install_author_hard_blocks)
    if demoted_hard_blocks:
        bp_meta["demoted_hard_blocks"] = list(demoted_hard_blocks)
    settings = dict(workspace.settings or {})
    settings["_blueprint"] = bp_meta
    workspace.settings = settings

    # ── Channel + session requirements → todo list ──
    await _bind_blueprint_channel_configs(
        db,
        workspace=workspace,
        channel_requirements=contract.get("channels") or [],
        selected_channel_config_ids=channel_config_ids or {},
        user_id=user_id,
    )
    from packages.core.services.workspace_readiness import (
        blueprint_channel_is_ready,
        list_configured_workspace_channels,
    )

    configured_channels = (
        await list_configured_workspace_channels(db, workspace)
        if contract.get("channels")
        else []
    )
    for index, req in enumerate(contract.get("channels") or []):
        requirement = _live_channel_requirement(index, req, configured_channels)
        live_setup_requirements.append(requirement)
        if blueprint_channel_is_ready(configured_channels, requirement.payload):
            continue
        result.todos.append(requirement)

    for req in contract.get("sessions") or []:
        requirement = InstallTodo(
            kind=BlueprintInstallTodoKind.BROWSER_SESSION.value,
            detail=(
                f"Capture a {req.get('provider')} browser session"
                + (f" labelled '{req.get('label')}'" if req.get("label") else "")
            ),
            payload=dict(req),
            blocking=bool(req.get("required", True)),
        )
        live_setup_requirements.append(requirement)
        session_stmt = select(IntegrationSession.id).where(
            IntegrationSession.entity_id == entity_id,
            IntegrationSession.status == "active",
        )
        provider = str(req.get("provider") or "").strip()
        label = str(req.get("label") or "").strip()
        if provider:
            session_stmt = session_stmt.where(
                IntegrationSession.provider == provider
            )
        if label:
            session_stmt = session_stmt.where(IntegrationSession.label == label)
        if (await db.execute(session_stmt.limit(1))).scalar_one_or_none() is not None:
            continue
        result.todos.append(requirement)

    # Persist the contract before post-install checks call the live evaluator.
    # Without this install-in-progress write, a fresh install is indistinguishable
    # from a legacy install that truly lacks the durable contract.
    _persist_install_todos(
        workspace,
        result.todos,
        live_setup_requirements,
    )

    # ── Post-install checks ──
    # Run each check inline; failures surface as blocking todos. The
    # blueprint says "you should be able to do X after install" — if X
    # doesn't work, the operator finds out NOW, not at first cron tick.
    for chk in policy.get("post_install_checks") or []:
        if isinstance(chk, dict) and str(chk.get("kind") or "").strip() in {
            "session_alive",
            "agent_callable",
            "cron_scheduled",
            "workflow_present",
            "workflow_dryrun",
        }:
            live_setup_requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    "Required Blueprint post-install check is no longer ready: "
                    f"{chk.get('kind')}."
                ),
                payload={"check": dict(chk)},
                blocking=True,
            ))
            _persist_install_todos(
                workspace,
                result.todos,
                live_setup_requirements,
            )
        await _run_post_install_check(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            check=chk,
            todos=result.todos,
        )

    _persist_install_todos(
        workspace,
        result.todos,
        live_setup_requirements,
    )

    # Persist the original requirements above so explicit integration.enable
    # restores them. The install receipt reflects only currently effective setup.
    result.todos = _effective_install_todos(workspace, result.todos)
    blocking_todos = [todo for todo in result.todos if todo.blocking]

    if mode == InstallMode.SIMULATE:
        if blocking_todos:
            result.notes.append(
                "Simulation installed with blocking setup. Complete the setup "
                "items before starting the walkthrough."
            )
        else:
            result.notes.append(
                "Simulation started — the first Strategist task proposal is "
                "auto-approved, plans default to dry_run, and measurements are "
                "simulated. Promote when ready."
            )
    else:
        if workspace.heartbeat_enabled:
            from packages.core.services.workspace_runtime import (
                install_workspace_runtime_schedules,
            )

            await install_workspace_runtime_schedules(
                db,
                workspace,
                cadence=workspace.heartbeat_cadence,
                user_id=user_id,
            )
            from packages.core.services.workspace_initial_review import prepare_initial_workspace_review

            await prepare_initial_workspace_review(db, workspace)
        if blocking_todos:
            result.notes.append(
                "Installed in LIVE mode with runtime startup suspended until "
                "blocking setup is complete."
            )
        else:
            result.notes.append(
                "Installed in LIVE mode — actions hit real systems."
            )

    from packages.core.blueprints.reconfigure import capture_install_baseline

    await capture_install_baseline(db, workspace=workspace, payload=payload)
    # Text the Blueprint ships in one language reads in each viewer's.
    from packages.core.services.workspace_translation import schedule_workspace_translation

    schedule_workspace_translation(workspace.id)
    return result


# ── Section installers ───────────────────────────────────────────────

async def _readable_legacy_candidates(
    db: AsyncSession,
    *,
    candidates: list[Any],
    entity_id: str,
    user_id: Optional[str],
    resource_type: str,
) -> list[Any]:
    """Filter legacy slug matches through the reusable-resource gateway."""
    if user_id is None:
        # Internal installs without a caller may adopt only the legacy
        # entity-visible catalog. A Workspace-scoped or private resource must
        # still have an explicit reference and an actor-bound authorization.
        return [
            candidate
            for candidate in candidates
            if candidate.entity_id is None or (
                not getattr(candidate, "workspace_id", None)
                and str(getattr(candidate, "visibility", None) or Visibility.ENTITY)
                in {Visibility.ENTITY, Visibility.PUBLIC}
            )
        ]
    entity_candidates = [
        candidate
        for candidate in candidates
        if candidate.entity_id == entity_id
    ]
    readable_ids = await readable_resource_ids(
        db,
        descriptors=[
            ResourceDescriptor.from_row(candidate, resource_type)
            for candidate in entity_candidates
        ],
        entity_id=entity_id,
        user_id=user_id,
    )
    return [
        candidate
        for candidate in candidates
        if candidate.entity_id is None or candidate.id in readable_ids
    ]


async def _install_subscription(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    sub: dict[str, Any], create_missing: bool,
    owner_user_id: Optional[str] = None,
    embedded_agent_ids: Optional[dict[str, str]] = None,
    selected_agent_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[InstallTodo]]:
    slug = sub.get("agent_slug")
    marketplace_agent_id = str(sub.get("marketplace_agent_id") or "").strip()
    if not slug and not marketplace_agent_id and not selected_agent_id:
        return None, InstallTodo(
            kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
            detail=(
                f"subscription {sub.get('service_key')!r} has no "
                "marketplace_agent_id or embedded agent_slug"
            ),
            payload=dict(sub), blocking=True,
        )
    agent = None
    embedded_agent_id = (embedded_agent_ids or {}).get(str(slug or ""))
    if selected_agent_id:
        # An installer's choice is a local runtime binding, not a portable
        # Blueprint identity. The UI lists readable entity Agents, and the
        # installer enforces the same rule for direct API callers.
        candidate = (await db.execute(
            select(Agent).where(
                Agent.id == selected_agent_id,
                Agent.entity_id == entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if candidate is None or candidate.id not in await readable_resource_ids(
            db,
            descriptors=[ResourceDescriptor.from_row(candidate, RESOURCE_AGENT)] if candidate else [],
            entity_id=entity_id,
            user_id=owner_user_id,
        ):
            raise InstallError(
                f"selected Agent for service {sub.get('service_key')!r} is unavailable"
            )
        agent = candidate
    elif marketplace_agent_id:
        from packages.core.services.marketplace_agent_service import (
            ensure_marketplace_agent_installed,
        )

        try:
            agent = await ensure_marketplace_agent_installed(
                db,
                entity_id=entity_id,
                agent_id=marketplace_agent_id,
                owner_user_id=owner_user_id,
            )
        except MarketplaceIdentityConflictError as exc:
            return None, InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                detail=str(exc),
                payload={
                    "marketplace_agent_id": marketplace_agent_id,
                    "agent_slug": slug,
                    "service_key": sub.get("service_key"),
                    "identity_conflict": str(exc),
                },
                blocking=True,
            )
        except ValueError:
            return None, InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                detail=(
                    f"Marketplace Agent id {marketplace_agent_id!r} is not available"
                ),
                payload={
                    "marketplace_agent_id": marketplace_agent_id,
                    "agent_slug": slug,
                    "service_key": sub.get("service_key"),
                },
                blocking=True,
            )
    elif embedded_agent_id:
        agent = (await db.execute(
            select(Agent).where(
                Agent.id == embedded_agent_id,
                Agent.entity_id == entity_id,
                Agent.workspace_id == workspace_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
    else:
        # Historical Blueprint payloads contain only a slug. Limit fallback to
        # one unambiguous row; new exports always carry an exact Marketplace id
        # for external Agents and an exact in-install id for embedded Agents.
        candidates = list((await db.execute(
            select(Agent).where(
                Agent.slug == slug,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
                or_(
                    Agent.entity_id == entity_id,
                    and_(
                        Agent.entity_id.is_(None),
                        Agent.is_template.is_(True),
                        Agent.is_public.is_(True),
                    ),
                ),
            )
        )).scalars().all())
        candidates = await _readable_legacy_candidates(
            db,
            candidates=candidates,
            entity_id=entity_id,
            user_id=owner_user_id,
            resource_type=RESOURCE_AGENT,
        )
        if len(candidates) == 1:
            agent = candidates[0]
        elif len(candidates) > 1:
            installed_candidates = [
                candidate
                for candidate in candidates
                if candidate.entity_id == entity_id
                and str(
                    (candidate.config or {}).get("source_agent_id") or ""
                ).strip()
            ]
            if len(installed_candidates) == 1:
                installed_source_id = str(
                    (installed_candidates[0].config or {}).get("source_agent_id")
                    or ""
                ).strip()
                other_source_ids = {
                    candidate.id
                    for candidate in candidates
                    if candidate.entity_id is None
                }
                if not other_source_ids or other_source_ids == {installed_source_id}:
                    agent = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate.entity_id is None
                            and candidate.id == installed_source_id
                        ),
                        installed_candidates[0],
                    )
            if agent is None:
                return None, InstallTodo(
                    kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                    detail=(
                        f"legacy agent slug {slug!r} is ambiguous; republish the "
                        "Blueprint with marketplace_agent_id"
                    ),
                    payload={"agent_slug": slug, "service_key": sub.get("service_key")},
                    blocking=True,
                )
    if agent is None:
        if not create_missing:
            return None, InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                detail=(
                    f"agent slug {slug!r} not installed locally — "
                    f"either install it or re-run with create_missing_agents=true "
                    f"to skip this subscription."
                ),
                payload={"agent_slug": slug, "service_key": sub.get("service_key")},
                blocking=True,
            )
        # A placeholder is an editing aid, not executable capacity. Creating
        # an active subscription here made readiness claim the service was
        # wired even though the Agent itself remained draft.
        agent = Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            name=slug.replace("-", " ").title(),
            slug=slug,
            system_prompt="(placeholder — installed from blueprint, please edit)",
            is_template=False,
            status="draft",
        )
        db.add(agent)
        await db.flush()
        return None, InstallTodo(
            kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
            detail=(
                f"Configure and activate placeholder agent {slug!r}, then bind "
                f"it to service {sub.get('service_key')!r}."
            ),
            payload={
                "agent_slug": slug,
                "service_key": sub.get("service_key"),
                "placeholder_agent_id": agent.id,
            },
            blocking=True,
        )

    if agent.entity_id is None:
        from packages.core.services.marketplace_agent_service import (
            ensure_marketplace_agent_installed,
        )

        try:
            agent = await ensure_marketplace_agent_installed(
                db,
                entity_id=entity_id,
                agent_id=agent.id,
                owner_user_id=owner_user_id,
            )
        except MarketplaceIdentityConflictError as exc:
            return None, InstallTodo(
                kind=BlueprintInstallTodoKind.MISSING_AGENT.value,
                detail=str(exc),
                payload={
                    "marketplace_agent_id": agent.id,
                    "agent_slug": slug,
                    "service_key": sub.get("service_key"),
                    "identity_conflict": str(exc),
                },
                blocking=True,
            )

    await lock_reusable_resource_reference(
        db,
        entity_id=entity_id,
        resource_type=RESOURCE_AGENT,
        resource_id=agent.id,
    )
    row = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent.id,
        workspace_id=workspace_id,
        service_key=sub.get("service_key"),
        custom_prompt=sub.get("custom_prompt"),
        config=dict(sub.get("config") or {}),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id, None


async def _install_custom_field(
    db: AsyncSession, *, entity_id: str, workspace_id: str, cf: dict[str, Any],
) -> str:
    row = CustomFieldDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=cf["name"],
        display_name=cf.get("display_name") or cf["name"],
        field_type=cf.get("field_type", "text"),
        target=cf.get("target", "task"),
        options=list(cf.get("options") or []),
        default_value=cf.get("default_value"),
        required=bool(cf.get("required", False)),
        sort_order=int(cf.get("sort_order", 0)),
        status="active",
    )
    db.add(row)
    await db.flush()
    return row.id


async def _install_goal(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    g: dict[str, Any], runtime_enabled: bool = False,
) -> str:
    """Delegate to ``goals.create_goal`` under the Workspace runtime switch."""
    from packages.core.goals import create_goal
    from packages.core.stats.service import get_stat_by_key

    deadline = None
    if g.get("deadline"):
        try:
            deadline = date.fromisoformat(str(g["deadline"]))
        except ValueError:
            logger.warning(
                "blueprint install: skipping unparseable deadline %r on goal %r",
                g.get("deadline"), g.get("title"),
            )

    measurement_source = g.get("measurement_source")

    linked_stat = None
    if g.get("stat_key"):
        linked_stat = await get_stat_by_key(
            db, workspace_id=workspace_id, key=str(g["stat_key"]),
        )
        if linked_stat is None:
            raise InstallError(
                f"goal {g.get('title')!r} references missing stat_key {g.get('stat_key')!r}"
            )

    goal = await create_goal(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        stat_id=linked_stat.id if linked_stat is not None else None,
        title=g["title"],
        goal_key=g.get("goal_key"),
        description=g.get("description"),
        metric_key=g.get("metric_key") or (linked_stat.key if linked_stat is not None else "completion"),
        target_value=Decimal(str(g["target_value"])),
        baseline_value=(
            Decimal(str(g["baseline_value"]))
            if g.get("baseline_value") is not None else None
        ),
        deadline=deadline,
        measurement_source=measurement_source,
        measurement_cadence=g.get("measurement_cadence"),
        priority=int(g.get("priority", 3)),
        install_schedule=runtime_enabled,
    )
    return goal.id


async def _install_stat(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    payload: dict[str, Any],
    mode: InstallMode,
) -> str:
    """Install a portable Stat definition without runtime observations."""
    from packages.core.stats.library import get_library_entry
    from packages.core.stats.service import create_stat, create_stat_from_library

    library_key = str(payload.get("library_key") or "").strip()
    entry = get_library_entry(library_key) if library_key else None
    install_schedule = not (
        mode == InstallMode.SIMULATE
        and (
            (entry is not None and entry.collector_type == "integration")
            or str(payload.get("collector_type") or "").strip() == "integration"
        )
    )
    if library_key:
        if entry is None:
            raise InstallError(f"unknown Blueprint stat library_key {library_key!r}")
        cadence_override = (
            {"collection_cadence": payload.get("collection_cadence")}
            if "collection_cadence" in payload else {}
        )
        stat = await create_stat_from_library(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            library_key=library_key,
            key=payload.get("key"),
            name=payload.get("name"),
            window=payload.get("window"),
            collector_overrides=payload.get("collector_config"),
            origin="blueprint",
            install_schedule=install_schedule,
            **cadence_override,
        )
    else:
        if not payload.get("key") or not payload.get("name"):
            raise InstallError("custom Blueprint stats require key and name")
        stat = await create_stat(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            key=str(payload["key"]),
            name=str(payload["name"]),
            description=payload.get("description"),
            value_type=str(payload.get("value_type") or "number"),
            unit=payload.get("unit"),
            window=str(payload.get("window") or "latest"),
            collector_type=str(payload.get("collector_type") or "manual"),
            collector_config=payload.get("collector_config"),
            collection_cadence=payload.get("collection_cadence"),
            freshness_limit_seconds=payload.get("freshness_limit_seconds"),
            origin="blueprint",
            goal_eligible=bool(payload.get("goal_eligible", True)),
            install_schedule=install_schedule,
        )
    return stat.id


async def _resolve_blueprint_skill_target(
    db: AsyncSession, *, entity_id: str, user_id: Optional[str],
    target: dict[str, Any], skill_id_by_component_key: dict[str, str],
) -> Skill:
    """Resolve exact portable identity before the ordinary resource lock."""
    component_key = str(
        target.get("skill_component_key") or ""
    ).strip()
    marketplace_skill_id = str(
        target.get("skill_marketplace_id") or ""
    ).strip()
    marketplace_source = str(
        target.get("skill_marketplace_source") or "platform"
    ).strip()
    skill_id = skill_id_by_component_key.get(component_key)
    skill = None
    if skill_id and not marketplace_skill_id:
        skill = (await db.execute(select(Skill).where(
            Skill.id == skill_id,
            Skill.entity_id == entity_id,
            Skill.status == "active",
        ))).scalar_one_or_none()
    elif marketplace_skill_id and marketplace_source == "manor":
        # The curated Manor marketplace bundle is Cloud-only. Keep this
        # branch syntactically valid when OSS export removes its body.
        skill = None
        skill_resolution_error: str | None = None
        if skill is None:
            detail = (
                f": {skill_resolution_error}"
                if skill_resolution_error else ""
            )
            raise InstallError(
                f"Blueprint Manor Skill {component_key!r} could not be "
                f"installed{detail}"
            )
    elif marketplace_skill_id and marketplace_source == "platform":
        from packages.core.services.marketplace_skill_service import (
            ensure_marketplace_skill_installed,
        )

        try:
            skill = await ensure_marketplace_skill_installed(
                db,
                entity_id=entity_id,
                skill_id=marketplace_skill_id,
                owner_user_id=user_id,
            )
        except (MarketplaceIdentityConflictError, ValueError) as exc:
            raise InstallError(
                f"Blueprint skill {component_key!r} could not be installed: {exc}"
            ) from exc
    elif component_key:
        # Compatibility for payloads written before exact component refs.
        candidates = list((await db.execute(select(Skill).where(
            Skill.slug == component_key,
            Skill.status == "active",
            or_(
                Skill.entity_id == entity_id,
                and_(
                    Skill.entity_id.is_(None),
                    Skill.is_public.is_(True),
                ),
            ),
        ))).scalars().all())
        candidates = await _readable_legacy_candidates(
            db,
            candidates=candidates,
            entity_id=entity_id,
            user_id=user_id,
            resource_type=RESOURCE_SKILL,
        )
        if len(candidates) == 1:
            skill = candidates[0]
            if skill.entity_id is None:
                from packages.core.services.marketplace_skill_service import (
                    ensure_marketplace_skill_installed,
                )

                try:
                    skill = await ensure_marketplace_skill_installed(
                        db,
                        entity_id=entity_id,
                        skill_id=skill.id,
                        owner_user_id=user_id,
                    )
                except (MarketplaceIdentityConflictError, ValueError) as exc:
                    raise InstallError(
                        f"Blueprint skill {component_key!r} could not be "
                        f"installed: {exc}"
                    ) from exc
    if skill is None:
        raise InstallError(
            "Blueprint skill requires a resolvable "
            "target.skill_component_key"
        )
    return skill


async def _install_scheduled_job(
    db: AsyncSession, *, entity_id: str, workspace_id: str,
    sj: dict[str, Any], user_id: Optional[str], mode: InstallMode,
    source_template_id: str,
    skill_id_by_component_key: Optional[dict[str, str]] = None,
) -> str:
    """Direct insert — the scheduler service has many bespoke create
    paths; we replicate the field set the blueprint exporter emitted."""
    # job_id is unique globally — scope it to this install so two
    # installs of the same blueprint don't collide.
    base_job_id = sj.get("job_id") or f"bp-{generate_ulid()[:8]}"
    job_id = installed_blueprint_job_id(base_job_id, workspace_id)
    execution_target = dict(sj.get("execution_target") or {})
    execution_type = sj.get("execution_type") or "agent"
    if execution_type == "agent_message":
        execution_type = "agent"

    if execution_type == "strategist_review":
        # Strategist cadence is Workspace-scoped. Persist the concrete
        # Workspace id so dispatch and Automation details expose its scope.
        execution_target["workspace_id"] = workspace_id

    if execution_type == "skill":
        skill = await _resolve_blueprint_skill_target(
            db, entity_id=entity_id, user_id=user_id, target=execution_target,
            skill_id_by_component_key=skill_id_by_component_key or {},
        )
        execution_target["skill_id"] = skill.id
        execution_target["workspace_id"] = workspace_id

    job_enabled = bool(sj.get("enabled", True))

    if execution_type == "workflow":
        workflow_slug = str(execution_target.get("workflow_slug") or "").strip()
        workflow_installation = (await db.execute(
            select(WorkflowTemplateInstallation).where(
                WorkflowTemplateInstallation.entity_id == entity_id,
                WorkflowTemplateInstallation.template_id == source_template_id,
                WorkflowTemplateInstallation.component_key == workflow_slug,
            ).limit(1)
        )).scalar_one_or_none()
        if workflow_installation is None:
            raise InstallError(
                f"scheduled workflow {workflow_slug!r} was not installed"
            )
        workflow = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.id == workflow_installation.workflow_id,
                WorkflowDefinition.workspace_id == workspace_id,
            ).limit(1)
        )).scalar_one_or_none()
        if workflow is None:
            raise InstallError(
                f"scheduled workflow {workflow_slug!r} was not installed"
            )
        workflow_id = workflow.id
        installation_metadata = (
            workflow_installation.installation_metadata
            if isinstance(workflow_installation.installation_metadata, dict)
            else {}
        )

        execution_target.update({
            "workflow_id": workflow_id,
            "workspace_id": workspace_id,
        })
        if installation_metadata.get("internal"):
            execution_target.pop("binding_id", None)
        else:
            from packages.core.services import workflow_service

            bindings = await workflow_service.list_bindings(
                db,
                entity_id,
                workspace_id=workspace_id,
                workflow_id=workflow_id,
            )
            binding = next(
                (
                    item for item in bindings
                    if item.enabled and item.status == "active"
                ),
                bindings[0] if bindings and not job_enabled else None,
            )
            if binding is None:
                raise InstallError(
                    f"scheduled workflow {workflow.name!r} has no active "
                    "Workspace binding"
                )
            execution_target["binding_id"] = binding.id

    agent_id = sj.get("agent_id")
    service_key = str(execution_target.get("service_key") or "").strip()
    if not agent_id and service_key:
        subscriptions = list((await db.execute(
            select(AgentSubscription).where(
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            ).order_by(AgentSubscription.id.asc()).limit(2)
        )).scalars().all())
        if not subscriptions:
            raise InstallError(
                f"scheduled agent service {service_key!r} has no active "
                "Workspace subscription"
            )
        if len(subscriptions) > 1:
            raise InstallError(
                f"scheduled agent service {service_key!r} is ambiguous in "
                "this Workspace"
            )
        sub = subscriptions[0]
        agent_id = sub.agent_id
        execution_target["agent_subscription_id"] = sub.id
    if execution_type == "agent" and not service_key:
        raise InstallError(
            "scheduled agent requires a portable execution_target.service_key"
        )
    if execution_type == "agent" and not agent_id:
        raise InstallError(
            f"scheduled agent service {service_key!r} has no active Workspace subscription"
        )

    agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(
        execution_target
    )
    if agent_id and not is_master_agent(agent_id):
        agent_ids.add(str(agent_id))
    await lock_reusable_resource_references(
        db,
        entity_id=entity_id,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        workflow_ids=workflow_ids,
    )

    row = ScheduledJob(
        id=generate_ulid(),
        job_id=job_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=sj.get("name"),
        job_type=sj.get("job_type", "cron"),
        schedule_kind=sj.get("schedule_kind"),
        cron_expr=sj.get("cron_expr"),
        every_seconds=sj.get("every_seconds"),
        run_at=sj.get("run_at"),
        stop_after=(datetime.fromisoformat(str(sj["stop_after"])) if sj.get("stop_after") else None),
        timezone=sj.get("timezone", "UTC"),
        payload_message=sj.get("payload_message"),
        agent_id=agent_id,
        execution_type=execution_type,
        execution_target=execution_target,
        execution_script=sj.get("execution_script"),
        default_delivery_mode=sj.get("default_delivery_mode"),
        user_id=user_id,
        # In simulate mode, jobs still tick — but the underlying actions
        # respect settings.sandbox so they don't reach external systems.
        enabled=job_enabled,
        delete_after_run=bool(sj.get("delete_after_run", False)),
    )
    from packages.core.services.product_growth import persist_scheduled_job

    await persist_scheduled_job(db, row)
    return row.id


# ── Embedded skills / agents / knowledge packs ────────────────────────


def normalize_skill_slug(value: str | None) -> str:
    """Normalize labels only for controlled pre-link upgrade fallback.

    Fresh install identity is always the exact Marketplace source link; this
    helper must never select a new install target by a mutable slug.
    """
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


async def _install_embedded_skill(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    source_blueprint_id: Optional[str],
    owner_user_id: Optional[str],
    sk: dict[str, Any],
) -> Optional[str]:
    """Create (or reuse) a Skill row from embedded.skills[].

    Idempotency is scoped by ``(Marketplace Blueprint id, Workspace id,
    component key)``. A same-slug Skill from another Blueprint or a user's
    local catalog is unrelated and must never be adopted.
    """
    slug = sk.get("slug")
    if not slug:
        logger.warning("blueprint install: embedded skill missing slug, skipping")
        return None

    component_key = str(
        sk.get("component_id") or sk.get("id") or slug
    ).strip()
    existing = None
    if source_blueprint_id:
        link = await get_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace_id,
            local_resource_type=RESOURCE_SKILL,
            component_key=component_key,
            for_update=True,
        )
        if link is not None:
            existing = (await db.execute(
                select(Skill).where(
                    Skill.id == link.local_resource_id,
                    Skill.entity_id == entity_id,
                    Skill.workspace_id == workspace_id,
                )
            )).scalar_one_or_none()
            if existing is None:
                await db.delete(link)
                await db.flush()
    if existing is not None:
        # M11: reconciling an already-installed skill only bumps its config
        # revision when the union actually widens the tool set or reactivates
        # a disabled skill — a repeat install of the same blueprint is a
        # no-op and must leave the revision (and the audit trail) alone.
        from packages.core.revisions import (
            SKILL_CONTENT_REVISION_FIELDS,
            bump_revision,
            content_patch_for,
        )
        merged_tools = sorted(set(existing.tools or []) | set(sk.get("tools") or []))
        content_patch = content_patch_for(
            existing,
            {"tools": merged_tools, "status": "active"},
            SKILL_CONTENT_REVISION_FIELDS,
        )
        existing.tools = merged_tools
        if existing.status != "active":
            existing.status = "active"
        if content_patch:
            await bump_revision(db, existing, patch=content_patch)
        logger.info(
            "blueprint install: skill %r already exists in entity as %r, reconciling",
            slug, existing.slug,
        )
        await db.flush()
        if source_blueprint_id:
            await record_marketplace_resource_link(
                db,
                entity_id=entity_id,
                marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
                marketplace_resource_id=source_blueprint_id,
                relationship=RELATIONSHIP_INSTALLED_COMPONENT,
                scope_type=SCOPE_WORKSPACE,
                scope_id=workspace_id,
                local_resource_type=RESOURCE_SKILL,
                local_resource_id=existing.id,
                component_key=component_key,
                marketplace_version=str(sk.get("version") or "1.0.0"),
                linked_by=owner_user_id,
                metadata={"source_slug": slug},
            )
        return existing.id

    row = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        visibility=Visibility.WORKSPACE,
        name=sk.get("name") or slug,
        slug=slug,
        display_name=sk.get("display_name") or sk.get("name") or slug,
        description=sk.get("description"),
        system_prompt=sk.get("system_prompt") or "",
        tools=list(sk.get("tools") or []),
        input_schema=dict(sk.get("input_schema") or {}),
        output_format=sk.get("output_format") or "text",
        category=sk.get("category"),
        tags=list(sk.get("tags") or []),
        is_public=False,  # embedded skills are entity-private by definition
        version=sk.get("version") or "1.0.0",
        config={
            **dict(sk.get("config") or {}),
            "source_blueprint_id": source_blueprint_id,
            "source_blueprint_component_key": component_key,
        },
        status="active",
    )
    db.add(row)
    await db.flush()
    if source_blueprint_id:
        await record_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace_id,
            local_resource_type=RESOURCE_SKILL,
            local_resource_id=row.id,
            component_key=component_key,
            marketplace_version=str(sk.get("version") or "1.0.0"),
            linked_by=owner_user_id,
            metadata={"source_slug": slug},
        )
    return row.id


async def _install_embedded_agent(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    source_blueprint_id: Optional[str],
    owner_user_id: Optional[str],
    a: dict[str, Any],
    skill_id_by_slug: dict[str, str],
    required_skill_refs: dict[str, list[dict[str, Any]]],
    required_mcp_by_slug: dict[str, bool],
    live_setup_requirements: list[InstallTodo],
    final_policy: WorkspacePolicy,
    todos: list[InstallTodo],
) -> Optional[str]:
    """Create (or reuse) an Agent row plus tool / MCP / skill bindings
    and starter memory.

    Governance check: every tool in ``tool_bindings`` is matched against
    ``final_policy.never_allow_actions`` (after stripping the ``tool.``
    prefix). A hit raises ``InstallError`` — the blueprint asks for an
    action that the operator's preset would always block, so installing
    it would deliver a dead agent.

    Missing MCP servers don't fail the install; they surface as
    InstallTodo so the operator can pair them post-install.
    """
    slug = a.get("slug")
    if not slug:
        logger.warning("blueprint install: embedded agent missing slug, skipping")
        return None

    # Governance preview check
    _enforce_governance_against_agent(a, final_policy)

    component_key = str(
        a.get("component_id") or a.get("id") or slug
    ).strip()
    existing = None
    if source_blueprint_id:
        link = await get_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace_id,
            local_resource_type=RESOURCE_AGENT,
            component_key=component_key,
            for_update=True,
        )
        if link is not None:
            existing = (await db.execute(
                select(Agent).where(
                    Agent.id == link.local_resource_id,
                    Agent.entity_id == entity_id,
                    Agent.workspace_id == workspace_id,
                    Agent.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if existing is None:
                await db.delete(link)
                await db.flush()
    if existing is not None:
        from packages.core.revisions import (
            AGENT_CONTENT_REVISION_FIELDS,
            bump_revision,
            content_patch_for,
        )
        agent = existing
        config = dict(agent.config or {})
        config.update(dict(a.get("config") or {}))
        config = normalize_agent_runtime_config(config)
        capabilities = sorted({
            str(capability).strip()
            for capability in (
                list(config.get("business_capabilities") or [])
                + list(a.get("business_capabilities") or [])
            )
            if str(capability or "").strip()
        })
        if capabilities:
            config["business_capabilities"] = capabilities
        # M11: same rule as skills — an idempotent re-install that produces
        # the identical merged config and an already-active agent must not
        # bump the revision.
        content_patch = content_patch_for(
            agent,
            {"config": config, "status": "active"},
            AGENT_CONTENT_REVISION_FIELDS,
        )
        agent.status = "active"
        agent.config = config
        if content_patch:
            await bump_revision(db, agent, patch=content_patch)
        logger.info(
            "blueprint install: agent %r already exists in entity, reconciling bindings",
            slug,
        )
    else:
        config = normalize_agent_runtime_config(a.get("config"))
        capabilities = sorted({
            str(capability).strip()
            for capability in a.get("business_capabilities") or []
            if str(capability or "").strip()
        })
        if capabilities:
            config["business_capabilities"] = capabilities
        agent = Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            visibility=Visibility.WORKSPACE,
            name=a.get("name") or slug,
            slug=slug,
            description=a.get("description"),
            system_prompt=a.get("system_prompt"),
            config={
                **config,
                "source_blueprint_id": source_blueprint_id,
                "source_blueprint_component_key": component_key,
            },
            is_template=False,
            is_public=False,  # embedded agents stay entity-private
            category=a.get("category"),
            tags=list(a.get("tags") or []),
            source="blueprint",
            status="active",
            version=a.get("version") or "1.0",
        )
        db.add(agent)
        await db.flush()

    if source_blueprint_id:
        await record_marketplace_resource_link(
            db,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id=source_blueprint_id,
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=workspace_id,
            local_resource_type=RESOURCE_AGENT,
            local_resource_id=agent.id,
            component_key=component_key,
            marketplace_version=str(a.get("version") or "1.0"),
            linked_by=owner_user_id,
            metadata={"source_slug": slug},
        )

    existing_tool_ids = set((await db.execute(
        select(AgentToolBinding.tool_id).where(AgentToolBinding.agent_id == agent.id)
    )).scalars().all())

    # Tool bindings — fail-fast on missing ToolDefinition (the exporter's
    # invariant says requires.tools should cover everything embedded
    # agents bind, so a missing tool means the target entity hasn't
    # caught up to the same catalog version).
    for tool_name in a.get("tool_bindings") or []:
        td = (await db.execute(
            select(ToolDefinition).where(ToolDefinition.name == tool_name)
        )).scalar_one_or_none()
        if td is None:
            raise InstallError(
                f"embedded agent {slug!r}: tool {tool_name!r} not in this "
                f"deployment's ToolDefinition catalog. Add the tool first or "
                f"drop it from the blueprint."
            )
        if td.id not in existing_tool_ids:
            db.add(AgentToolBinding(agent_id=agent.id, tool_id=td.id))
            existing_tool_ids.add(td.id)

    existing_mcp_bindings = {
        row.mcp_server_id: row
        for row in (await db.execute(
            select(AgentMCPBinding).where(AgentMCPBinding.agent_id == agent.id)
        )).scalars().all()
    }

    # MCP bindings — missing server becomes a todo, not a failure.
    for binding in a.get("mcp_bindings") or []:
        if not isinstance(binding, dict):
            continue
        server_slug = binding.get("server_slug")
        if not server_slug:
            continue
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.server_key == server_slug)
        )).scalar_one_or_none()
        normalized_server_slug = str(server_slug).strip()
        required_config_fields = sorted({
            str(field).strip()
            for field in binding.get("config_override_allowlist") or []
            if str(field).strip()
        })
        binding_is_required = required_mcp_by_slug.get(normalized_server_slug, True)
        declared_allowed_tools = binding.get("allowed_tools")
        installed_allowed_tools = (
            None
            if declared_allowed_tools is None
            else list(declared_allowed_tools)
        )
        user_facing_provider = is_user_facing_integration_provider(server_slug)
        if user_facing_provider:
            live_setup_requirements.append(InstallTodo(
                kind=BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
                detail=(
                    f"Restore the {server_slug!r} MCP binding for agent {slug!r}."
                ),
                payload={
                    "server_slug": server_slug,
                    "agent_slug": slug,
                    "installed_agent_id": agent.id,
                    "allowed_tools": declared_allowed_tools,
                    "required_config_fields": required_config_fields,
                },
                blocking=binding_is_required,
            ))
        if srv is None:
            if user_facing_provider:
                todos.append(InstallTodo(
                    kind=BlueprintInstallTodoKind.MCP_SERVER.value,
                    detail=(
                        f"Install the {server_slug!r} MCP server, then bind it to "
                        f"agent {slug!r}. The blueprint expects these fields to be "
                        f"set on the binding: "
                        f"{list(binding.get('config_override_allowlist') or []) or '(none)'}"
                    ),
                    payload={
                        "server_slug": server_slug,
                        "agent_slug": slug,
                        "allowed_tools": binding.get("allowed_tools"),
                        "config_override_allowlist": binding.get("config_override_allowlist"),
                        "required_config_fields": required_config_fields,
                    },
                    blocking=binding_is_required,
                ))
            continue
        existing_mcp = existing_mcp_bindings.get(srv.id)
        if existing_mcp is not None:
            existing_mcp.allowed_tools = installed_allowed_tools
            existing_mcp.status = "active"
        else:
            new_binding = AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent.id,
                mcp_server_id=srv.id,
                allowed_tools=installed_allowed_tools,
                # config_override starts empty — the operator fills in the
                # allowlisted fields via UI (or the MCP setup flow).
                config_override={},
                status="active",
            )
            db.add(new_binding)
            existing_mcp_bindings[srv.id] = new_binding

        installed_binding = existing_mcp_bindings[srv.id]
        configured_fields = dict(installed_binding.config_override or {})
        missing_config_fields = [
            field
            for field in required_config_fields
            if field not in configured_fields
            or configured_fields[field] is None
            or (
                isinstance(configured_fields[field], str)
                and not configured_fields[field].strip()
            )
        ]
        if missing_config_fields and user_facing_provider:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
                detail=(
                    f"Configure {', '.join(missing_config_fields)} on MCP server "
                    f"{server_slug!r} for agent {slug!r}."
                ),
                payload={
                    "server_slug": server_slug,
                    "agent_slug": slug,
                    "allowed_tools": binding.get("allowed_tools"),
                    "required_config_fields": missing_config_fields,
                },
                blocking=binding_is_required,
            ))

    existing_skill_bindings = {
        row.skill_id: row
        for row in (await db.execute(
            select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id)
        )).scalars().all()
    }

    # Exact external refs take precedence. ``skill_bindings`` remains the
    # portable embedded-component/legacy shape; a matching exact ref suppresses
    # its slug entry so two Marketplace Skills sharing a slug never alias.
    exact_binding_refs = [
        ref for ref in (a.get("skill_binding_refs") or [])
        if isinstance(ref, dict)
        and str(ref.get("marketplace_id") or "").strip()
    ]
    exact_binding_slugs = {
        str(ref.get("slug") or "").strip()
        for ref in exact_binding_refs
        if str(ref.get("slug") or "").strip()
    }
    binding_refs: list[Any] = [*exact_binding_refs]
    binding_refs.extend(
        ref for ref in (a.get("skill_bindings") or [])
        if str(ref or "").strip() not in exact_binding_slugs
    )
    for binding_ref in binding_refs:
        exact_ref = binding_ref if isinstance(binding_ref, dict) else {}
        sk_slug = str(
            exact_ref.get("slug") if exact_ref else binding_ref
        ).strip()
        if not sk_slug:
            continue
        marketplace_skill_id = ""
        skill_id = None if exact_ref else skill_id_by_slug.get(sk_slug)
        if skill_id is None:
            requirement_candidates = required_skill_refs.get(sk_slug) or []
            requirement = (
                requirement_candidates[0]
                if len(requirement_candidates) == 1 else {}
            )
            marketplace_skill_id = str(
                exact_ref.get("marketplace_id")
                or requirement.get("marketplace_id")
                or ""
            ).strip()
            marketplace_source = str(
                exact_ref.get("marketplace_source")
                or requirement.get("marketplace_source")
                or "platform"
            ).strip()
            sk_row = None
            skill_resolution_error: str | None = None
            if marketplace_skill_id and marketplace_source == "manor":
                # The curated Manor marketplace bundle is Cloud-only. Keep the
                # branch valid after OSS export strips its implementation.
                sk_row = None
            elif marketplace_skill_id and marketplace_source == "platform":
                from packages.core.services.marketplace_skill_service import (
                    ensure_marketplace_skill_installed,
                )

                try:
                    sk_row = await ensure_marketplace_skill_installed(
                        db,
                        entity_id=entity_id,
                        skill_id=marketplace_skill_id,
                        owner_user_id=owner_user_id,
                    )
                except MarketplaceIdentityConflictError as exc:
                    skill_resolution_error = str(exc)
                    sk_row = None
                except ValueError:
                    sk_row = None
            elif not marketplace_skill_id:
                # Controlled compatibility for old slug-only payloads. Never
                # choose among multiple Marketplace/local Skills.
                candidates = list((await db.execute(
                    select(Skill).where(
                        Skill.slug == sk_slug,
                        Skill.status == "active",
                        or_(
                            Skill.entity_id == entity_id,
                            and_(
                                Skill.entity_id.is_(None),
                                Skill.is_public.is_(True),
                            ),
                        ),
                    )
                )).scalars().all())
                candidates = await _readable_legacy_candidates(
                    db,
                    candidates=candidates,
                    entity_id=entity_id,
                    user_id=owner_user_id,
                    resource_type=RESOURCE_SKILL,
                )
                if len(candidates) == 1:
                    sk_row = candidates[0]
                elif len(candidates) > 1:
                    installed_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.entity_id == entity_id
                        and str(
                            (candidate.config or {}).get("source_skill_id") or ""
                        ).strip()
                    ]
                    if len(installed_candidates) == 1:
                        installed_source_id = str(
                            (installed_candidates[0].config or {}).get(
                                "source_skill_id"
                            )
                            or ""
                        ).strip()
                        marketplace_source_ids = {
                            candidate.id
                            for candidate in candidates
                            if candidate.entity_id is None
                        }
                        if (
                            not marketplace_source_ids
                            or marketplace_source_ids == {installed_source_id}
                        ):
                            sk_row = next(
                                (
                                    candidate
                                    for candidate in candidates
                                    if candidate.entity_id is None
                                    and candidate.id == installed_source_id
                                ),
                                installed_candidates[0],
                            )
                if sk_row is not None and sk_row.entity_id is None:
                    from packages.core.services.marketplace_skill_service import (
                        ensure_marketplace_skill_installed,
                    )

                    try:
                        sk_row = await ensure_marketplace_skill_installed(
                            db,
                            entity_id=entity_id,
                            skill_id=sk_row.id,
                            owner_user_id=owner_user_id,
                        )
                    except MarketplaceIdentityConflictError as exc:
                        skill_resolution_error = str(exc)
                        sk_row = None
                    except ValueError:
                        sk_row = None
            if sk_row is None:
                todos.append(InstallTodo(
                    kind=BlueprintInstallTodoKind.MISSING_SKILL.value,
                    detail=(
                        f"Skill {sk_slug!r} required by agent {slug!r} has a "
                        f"Marketplace identity conflict: {skill_resolution_error}"
                        if skill_resolution_error
                        else f"Skill {sk_slug!r} required by agent {slug!r} is not "
                        f"installed in this deployment. Install it, then bind "
                        f"to the agent manually."
                    ),
                    payload={
                        "skill_slug": sk_slug,
                        "marketplace_skill_id": marketplace_skill_id or None,
                        **({"marketplace_source": marketplace_source} if marketplace_skill_id else {}),
                        "agent_slug": slug,
                        "installed_agent_id": agent.id,
                        **(
                            {"identity_conflict": skill_resolution_error}
                            if skill_resolution_error else {}
                        ),
                    },
                    blocking=True,
                ))
                continue
            skill_id = sk_row.id
        existing_skill = existing_skill_bindings.get(skill_id)
        if existing_skill is not None:
            existing_skill.status = "active"
        else:
            await lock_agent_skill_binding_references(
                db,
                entity_id=entity_id,
                agent_id=agent.id,
                skill_id=skill_id,
            )
            new_binding = AgentSkillBinding(
                id=generate_ulid(),
                agent_id=agent.id,
                skill_id=skill_id,
                status="active",
            )
            db.add(new_binding)
            existing_skill_bindings[skill_id] = new_binding
        live_setup_requirements.append(InstallTodo(
            kind=BlueprintInstallTodoKind.MISSING_SKILL.value,
            detail=f"Restore skill {sk_slug!r} for agent {slug!r}.",
            payload={
                "skill_slug": sk_slug,
                "marketplace_skill_id": marketplace_skill_id or None,
                **({"marketplace_source": marketplace_source} if marketplace_skill_id else {}),
                "installed_skill_id": skill_id,
                "agent_slug": slug,
                "installed_agent_id": agent.id,
            },
            blocking=True,
        ))

    # Starter memory is scoped to this Blueprint install's Workspace.
    existing_memory_keys = {
        (row.memory_type, row.scope, row.content)
        for row in (await db.execute(
            select(AgentMemory).where(
                AgentMemory.agent_id == agent.id,
                AgentMemory.user_id.is_(None),
                AgentMemory.workspace_id == workspace_id,
                AgentMemory.status == "active",
            )
        )).scalars().all()
    }
    for m in a.get("starter_memory") or []:
        if not isinstance(m, dict) or "user_id" in m and m["user_id"] is not None:
            # validate_payload already rejected user_id, but be defensive.
            continue
        memory_key = (
            m.get("memory_type") or "instruction",
            m.get("scope"),
            m.get("content") or "",
        )
        if memory_key in existing_memory_keys:
            continue
        db.add(AgentMemory(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent.id,
            user_id=None,
            workspace_id=workspace_id,
            memory_type=m.get("memory_type") or "instruction",
            scope=m.get("scope"),
            content=m.get("content") or "",
            importance=int(m.get("importance") or 5),
            confidence=float(m.get("confidence") or 1.0),
            source="blueprint",
            metadata_={
                "installed_with_agent_slug": slug,
                "source_blueprint_id": source_blueprint_id,
                "source_blueprint_component_key": component_key,
            },
            status="active",
        ))
        existing_memory_keys.add(memory_key)

    await db.flush()
    return agent.id


def _enforce_governance_against_agent(
    a: dict[str, Any], final_policy: WorkspacePolicy,
) -> None:
    """Raise InstallError if any tool the agent binds would be hard-blocked
    by the post-preset governance policy. Tool names are matched by glob
    against ``never_allow_actions`` after stripping the ``tool.`` prefix
    (so ``tool.x.delete_account`` matches ``x.delete_*``)."""
    from packages.core.governance.approval_projection import policy_override
    rules = policy_override(final_policy).rules
    for tool in a.get("tool_bindings") or []:
        if not isinstance(tool, str):
            continue
        action_form = tool.removeprefix("tool.")
        for rule in rules:
            if rule.effect == "deny" and rule.selector.kind == "action" and rule.selector.action_key in {tool, action_form}:
                raise InstallError(f"embedded agent {a.get('slug')!r} binds tool {tool!r} prohibited by {rule.id}")


async def _install_knowledge_pack(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    kp: dict[str, Any],
    todos: list[InstallTodo],
) -> Optional[str]:
    """Create a DocumentGroup and materialize its inline starter documents.

    Text is indexed in ``metadata.content_text`` and, when filesystem storage
    is enabled, materialized at the Workspace path for native file tools.
    """
    slug = kp.get("slug")
    title = kp.get("title") or slug
    if not title:
        return None

    # Installed Blueprint slugs are the portable identity. Display titles are
    # not unique: the exporter assigns distinct slugs to same-title groups,
    # and reusing by title would silently drop every later group.
    slug_groups = list((await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
            DocumentGroup.settings["installed_from_blueprint_slug"].astext == slug,
        )
    )).scalars().all()) if slug else []
    if len(slug_groups) > 1:
        raise InstallError(f"Knowledge pack slug {slug!r} is ambiguous")
    existing = slug_groups[0] if slug_groups else None
    same_title_groups = list((await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
            DocumentGroup.name == title,
        )
    )).scalars().all())
    if existing is None:
        # Retain the legacy idempotency behavior for a pre-existing local
        # group that has not yet been assigned a Blueprint pack identity.
        legacy_groups = [
            group for group in same_title_groups
            if not isinstance(group.settings, dict)
            or not group.settings.get("installed_from_blueprint_slug")
        ]
        if len(legacy_groups) > 1:
            raise InstallError(f"Knowledge pack title {title!r} is ambiguous")
        existing = legacy_groups[0] if legacy_groups else None
    if existing is not None:
        group_id = existing.id
    else:
        group = DocumentGroup(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            name=title,
            settings={
                "purpose": kp.get("purpose"),
                "folder_structure": list(kp.get("folder_structure") or []),
                "mode": kp.get("mode") or "skeleton",
                "external_source": kp.get("external_source"),
                "installed_from_blueprint_slug": slug,
            },
        )
        db.add(group)
        await db.flush()
        group_id = group.id

    # Inline_text mode → create real, immediately readable Knowledge rows.
    # This is idempotent and never overwrites an existing document, so a
    # second install cannot discard edits made after the first install.
    if kp.get("mode") == "inline_text":
        for d in kp.get("starter_documents") or []:
            if not isinstance(d, dict):
                continue
            await _materialize_knowledge_pack_document(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                group_id=group_id,
                knowledge_pack_slug=str(slug or ""),
                document=d,
            )
    elif list(kp.get("folder_structure") or []):
        todos.append(InstallTodo(
            kind=BlueprintInstallTodoKind.KNOWLEDGE_PACK_CONTENT.value,
            detail=(
                f"Add source content to Knowledge pack {title!r}; this "
                "Blueprint exported its structure without document bodies."
            ),
            payload={
                "knowledge_pack_slug": slug,
                "knowledge_pack_title": title,
                "document_group_id": group_id,
            },
            blocking=True,
        ))

    return group_id


async def _knowledge_pack_document_rows(
    db: AsyncSession,
    *,
    entity_id: str,
    group_id: str,
    knowledge_pack_slug: str,
    documents: list[dict[str, Any]],
) -> list[Document]:
    """Read only candidate starter identities, never a whole Knowledge pack."""
    if not documents:
        return []
    # Bound each identity separately: duplicate matches for one starter must
    # not crowd another starter out of the upgrade preview.
    candidates = union_all(*[
        select(Document.id).where(
            Document.entity_id == entity_id,
            Document.id.in_(select(DocumentGroupMember.document_id).where(
                DocumentGroupMember.group_id == group_id,
            )),
            Document.is_trashed.is_(False),
            _blueprint_starter_document_predicate(document, knowledge_pack_slug),
        ).order_by(Document.id).limit(2)
        for document in documents
    ]).subquery()
    return list((await db.execute(
        select(Document).where(Document.id.in_(select(candidates.c.id))).order_by(Document.id)
    )).scalars().all())


async def _workspace_blueprint_document_rows(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    document_key: str,
) -> list[Document]:
    """Resolve one portable key, bounded to two rows to detect ambiguity."""

    result = await db.execute(
        select(Document)
        .where(
            Document.entity_id == entity_id,
            Document.id.in_(
                select(DocumentGroupMember.document_id)
                .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
                .where(
                    DocumentGroup.entity_id == entity_id,
                    DocumentGroup.workspace_id == workspace_id,
                )
            ),
            Document.is_trashed == False,  # noqa: E712
            Document.metadata_["blueprint_document_key"].astext == document_key,
        ).order_by(Document.id).limit(2)
    )
    return list(result.unique().scalars().all())


def _blueprint_starter_document_predicate(document: dict[str, Any], pack_slug: str):
    key = str(document.get("key") or "").strip()
    if key:
        return Document.metadata_["blueprint_document_key"].astext == key
    path = str(document.get("path") or "").strip()
    return and_(
        Document.metadata_["blueprint_knowledge_pack_slug"].astext == pack_slug,
        Document.metadata_["blueprint_starter_path"].astext == path,
    )


async def _require_blueprint_document_owned_by_workspace(
    db: AsyncSession, row: Document, workspace_id: str,
) -> None:
    from packages.core.services.document_access import document_workspace_ids

    await db.refresh(row, with_for_update=True)
    owners = await document_workspace_ids(db, row)
    unscoped_group = (await db.execute(
        select(DocumentGroupMember.group_id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(
            DocumentGroupMember.document_id == row.id,
            DocumentGroup.workspace_id.is_(None),
        ).limit(1)
    )).scalar_one_or_none()
    if owners != {workspace_id} or unscoped_group is not None:
        raise InstallError(
            "Cannot update a live Blueprint template shared with another Workspace "
            "or entity Knowledge group; create a Workspace-owned copy first."
        )


def _matches_blueprint_starter_document(
    row: Document,
    *,
    knowledge_pack_slug: str,
    path: str,
    document_key: str = "",
) -> bool:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    if document_key:
        return metadata.get("blueprint_document_key") == document_key
    return (
        (
            metadata.get("blueprint_knowledge_pack_slug") == knowledge_pack_slug
            and metadata.get("blueprint_starter_path") == path
        )
    )


async def _knowledge_starter_path_is_occupied(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    group_id: str,
    path: str,
) -> bool:
    """Whether an unproven row already occupies a starter's canonical path."""
    parent_id: str | None = None
    workspace = await db.get(Workspace, workspace_id)
    if (
        workspace is not None
        and workspace.entity_id == entity_id
        and workspace.artifact_folder_id
    ):
        parent_id = workspace.artifact_folder_id
        for part in (path.rsplit("/", 1)[0].split("/") if "/" in path else []):
            folder = (await db.execute(
                select(DocumentFolder).where(
                    DocumentFolder.entity_id == entity_id,
                    DocumentFolder.parent_id == parent_id,
                    DocumentFolder.name == part,
                ).order_by(
                    DocumentFolder.owner_id.isnot(None),
                    DocumentFolder.id,
                ).limit(1)
            )).scalar_one_or_none()
            if folder is None:
                parent_id = None
                break
            parent_id = folder.id
    conditions = [and_(
        Document.name == path,
        Document.folder_id.is_(None),
        Document.id.in_(select(DocumentGroupMember.document_id).where(
            DocumentGroupMember.group_id == group_id,
        )),
    )]
    if parent_id is not None:
        conditions.append(and_(
            Document.name == path.rsplit("/", 1)[-1],
            Document.folder_id == parent_id,
        ))
    return (await db.execute(
        select(Document.id).where(
            Document.entity_id == entity_id,
            Document.is_trashed.is_(False),
            or_(*conditions),
        ).order_by(Document.id).limit(1)
    )).scalar_one_or_none() is not None


def _blueprint_document_template(document: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return normalize_knowledge_template(document.get("template"))
    except ValueError as exc:
        raise InstallError(str(exc)) from exc


async def _materialize_knowledge_pack_document(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    group_id: str,
    knowledge_pack_slug: str,
    document: dict[str, Any],
) -> tuple[Optional[Document], bool]:
    """Ensure one inline Blueprint document exists without overwriting it."""
    from packages.core.services.knowledge_visibility import normalize_rel_path
    from packages.core.services.workspace_artifacts import ensure_workspace_document_folder
    from packages.core.services.knowledge_sync import repair_workspace_document_projection

    path = normalize_rel_path(str(document.get("path") or "").strip())
    body = str(document.get("body_md") or "")
    document_key = str(document.get("key") or "").strip()
    if not path or not body:
        return None, False

    rows = await _knowledge_pack_document_rows(
        db, entity_id=entity_id, group_id=group_id,
        knowledge_pack_slug=knowledge_pack_slug, documents=[document],
    )
    template = _blueprint_document_template(document)
    initial_body = (
        {INITIAL_BODY_KEY: body}
        if template and template["mode"] == "initial_file"
        else {}
    )
    if document_key:
        workspace_rows = await _workspace_blueprint_document_rows(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            document_key=document_key,
        )
        matching_rows = [
            row
            for row in workspace_rows
            if _matches_blueprint_starter_document(
                row,
                knowledge_pack_slug=knowledge_pack_slug,
                path=path,
                document_key=document_key,
            )
        ]
        if len(matching_rows) > 1:
            raise InstallError(
                f"Blueprint Knowledge document key {document_key!r} is already ambiguous"
            )
        if matching_rows:
            row = matching_rows[0]
            if template is not None:
                await _require_blueprint_document_owned_by_workspace(db, row, workspace_id)
            if all(candidate.id != row.id for candidate in rows):
                db.add(DocumentGroupMember(document_id=row.id, group_id=group_id))
            if template is not None:
                row.metadata_ = merge_document_metadata(
                    row.metadata_ if isinstance(row.metadata_, dict) else {},
                    origin={"workspace_id": workspace_id},
                    extra={
                        "blueprint_document_key": document_key,
                        "blueprint_template": template,
                        **initial_body,
                    },
                )
            await db.flush()
            await repair_workspace_document_projection(
                db, entity_id=entity_id, workspace_id=workspace_id, document_ids=(row.id,),
            )
            return row, False

    for row in rows:
        if _matches_blueprint_starter_document(
            row,
            knowledge_pack_slug=knowledge_pack_slug,
            path=path,
            document_key=document_key,
        ):
            # Template configuration never overwrites the operator's current
            # file. Initial-file content is a separate export/install seed.
            if template is not None:
                await _require_blueprint_document_owned_by_workspace(db, row, workspace_id)
                row.metadata_ = merge_document_metadata(
                    row.metadata_ if isinstance(row.metadata_, dict) else {},
                    origin={"workspace_id": workspace_id},
                    extra={
                        "blueprint_knowledge_pack_slug": knowledge_pack_slug,
                        "blueprint_starter_path": path,
                        "blueprint_template": template,
                        **initial_body,
                    },
                )
            await repair_workspace_document_projection(
                db, entity_id=entity_id, workspace_id=workspace_id, document_ids=(row.id,),
            )
            return row, False

    if await _knowledge_starter_path_is_occupied(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        group_id=group_id,
        path=path,
    ):
        raise InstallError(
            f"Blueprint Knowledge starter path {path!r} is already occupied by an unproven document"
        )

    folder_id = await ensure_workspace_document_folder(
        entity_id=entity_id,
        workspace_id=workspace_id,
        rel_path=path,
        db=db,
    )
    row = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name=path.rsplit("/", 1)[-1],
        file_size=len(body.encode("utf-8")),
        file_type="csv" if path.endswith(".csv") else "md",
        mime_type="text/csv" if path.endswith(".csv") else "text/markdown",
        vector_status=VectorStatus.PENDING,
        source="blueprint",
        visibility="workspace",
        classification="public",
        folder_id=folder_id,
        metadata_=merge_document_metadata(
            origin={"workspace_id": workspace_id},
            extra={
                "content_text": body,
                "blueprint_knowledge_pack_slug": knowledge_pack_slug,
                "blueprint_starter_path": path,
                **(
                    {"blueprint_document_key": document_key}
                    if document_key
                    else {}
                ),
                **({"blueprint_template": template} if template is not None else {}),
                **initial_body,
            },
        ),
    )
    from packages.core.config import get_settings

    if get_settings().MANOR_FS_ENABLED:
        from packages.core.blueprints.knowledge_files import create_starter_file
        from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.entity_id != entity_id or not workspace.artifact_folder_id:
            raise InstallError("Blueprint starter file requires a Workspace artifact folder")
        row.fs_path = f"{workspace_artifact_storage_base(workspace.artifact_folder_id)}/{path}"
        from packages.core.services.entity_fs import EntityFilesystemError

        try:
            create_starter_file(db, entity_id=entity_id, path=row.fs_path, body=body)
        except (ValueError, EntityFilesystemError) as exc:
            raise InstallError(f"Could not create Blueprint starter {path!r}: {exc}") from exc
    db.add(row)
    db.add(DocumentGroupMember(document_id=row.id, group_id=group_id))
    await db.flush()
    return row, True


# ── Workflows ─────────────────────────────────────────────────────────


def _blueprint_workflow_trigger_config(w: dict[str, Any]) -> dict[str, Any]:
    """Build the portable trigger config used by definitions and bindings."""
    trigger_config = dict(w.get("trigger_config") or {})
    if w.get("trigger_ref"):
        trigger_config["trigger_ref"] = w["trigger_ref"]
    return trigger_config


def _blueprint_workflow_definition_values(w: dict[str, Any]) -> dict[str, Any]:
    """Translate one blueprint workflow into WorkflowDefinition values.

    Kept pure so install and blueprint-upgrade preview compare against the
    exact same runtime graph. A second translator here would eventually make
    an upgrade claim a Flow is current while installing something different.
    """
    slug = w.get("slug")
    if not slug:
        raise InstallError("workflow is missing slug")

    # Translate blueprint workflow DSL into the canonical runtime graph:
    # kind → type, depends_on → next, and an explicit trigger entry node.
    bp_steps = list(w.get("steps") or [])
    next_map: dict[str, list[str]] = {}
    for s in bp_steps:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not sid:
            continue
        for dep in s.get("depends_on") or []:
            next_map.setdefault(dep, []).append(sid)

    runtime_steps: list[dict[str, Any]] = []
    for s in bp_steps:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        sid = s["id"]
        step_type = _blueprint_runtime_step_type(s)
        config = _blueprint_runtime_step_config(s, step_type)
        has_explicit_routes = any(key in s for key in ("true_next", "false_next"))
        if step_type == "switch":
            has_explicit_routes = has_explicit_routes or bool(
                config.get("cases") or config.get("default_next")
            )
        step = {
            "id": sid,
            "type": step_type,
            "name": s.get("name") or sid,
            "config": config,
            "next": (
                _workflow_targets(s.get("next"))
                if "next" in s
                else []
                if has_explicit_routes
                else next_map.get(sid, [])
            ),
        }
        for route_key in ("true_next", "false_next"):
            if route_key in s:
                step[route_key] = _workflow_targets(s.get(route_key))
        if s.get("meta"):
            step["meta"] = s["meta"]
        if "chat_projection" in s:
            step["chat_projection"] = deepcopy(s["chat_projection"])
        if isinstance(s.get("position"), dict):
            position = s["position"]
            if all(
                isinstance(position.get(axis), (int, float))
                and not isinstance(position.get(axis), bool)
                for axis in ("x", "y")
            ):
                step["position"] = {
                    "x": position["x"],
                    "y": position["y"],
                }
        runtime_steps.append(step)

    if runtime_steps and not any(
        step.get("type") in ("trigger", "webhook") for step in runtime_steps
    ):
        existing_ids = {str(step.get("id")) for step in runtime_steps}
        start_id = "start" if "start" not in existing_ids else "blueprint_start"
        root_ids = [
            str(s.get("id"))
            for s in bp_steps
            if isinstance(s, dict) and s.get("id") and not s.get("depends_on")
        ]
        if not root_ids and runtime_steps:
            root_ids = [str(runtime_steps[0]["id"])]
        trigger_config = {
            "trigger_type": w.get("trigger_type") or "manual",
            **_blueprint_workflow_trigger_config(w),
        }
        # How the run's production shows travels on the trigger, beside its inputs.
        if isinstance(w.get("production"), dict):
            trigger_config["production"] = dict(w["production"])
        if isinstance(w.get("run_inputs"), list):
            run_inputs = [
                dict(item)
                for item in w["run_inputs"]
                if isinstance(item, dict)
            ]
            trigger_config["run_inputs"] = run_inputs
            trigger_config["outputs"] = [
                {
                    "key": str(item.get("key") or item.get("name")).strip(),
                    "type": (
                        "text"
                        if str(item.get("type") or "string").lower() == "string"
                        else str(item.get("type") or "any").lower()
                    ),
                    "value": (
                        "{{"
                        + start_id
                        + "."
                        + str(item.get("key") or item.get("name")).strip()
                        + "}}"
                    ),
                }
                for item in run_inputs
                if str(item.get("key") or item.get("name") or "").strip()
            ]
        runtime_steps.insert(0, {
            "id": start_id,
            "type": "trigger",
            "name": "Workflow start",
            "config": trigger_config,
            "next": root_ids,
        })

    if w.get("explicit_data_contracts"):
        from packages.core.services.workflow_contracts import (
            explicit_workflow_step_contracts,
        )

        runtime_steps = explicit_workflow_step_contracts(runtime_steps)

    from packages.core.services.workflow_service import validate_workflow_steps

    validation = validate_workflow_steps(runtime_steps)
    if not validation["valid"]:
        messages = "; ".join(error["message"] for error in validation["errors"])
        raise InstallError(f"workflow {slug!r} is invalid: {messages}")

    # Convert variables list → dict {key: default_value}.
    variables_dict: dict[str, Any] = {}
    for v in w.get("variables") or []:
        if isinstance(v, dict) and v.get("key"):
            variables_dict[v["key"]] = v.get("default")
    trigger_config = _blueprint_workflow_trigger_config(w)

    return {
        # Definitions normally keep the stable slug as their technical name.
        # A Blueprint may opt into a separate editor-facing label while source
        # identity remains anchored by WorkflowTemplateInstallation.
        "name": w.get("definition_name") or slug,
        "description": w.get("description"),
        "trigger_type": w.get("trigger_type") or "manual",
        "trigger_config": trigger_config,
        "steps": runtime_steps,
        "variables": variables_dict,
        "category": w.get("category"),
        "tags": list(w.get("tags") or []),
        "is_active": bool(w.get("definition_enabled", w.get("enabled", True))),
        "version": int(w.get("version") or 1),
        "status": str(
            w.get("definition_status") or w.get("status") or "active"
        ),
    }


async def _materialize_workflow_document_refs(
    db: AsyncSession, *, entity_id: str, workspace_id: Optional[str], node: Any,
) -> Any:
    try:
        keys = WorkflowDocumentDependencyFactory.portable_keys(node)
    except WorkflowDependencyError as exc:
        raise InstallError(str(exc)) from exc
    if not keys:
        return deepcopy(node)
    if not workspace_id:
        raise InstallError("Workflow Knowledge references require a Workspace")
    ids = {}
    for key in keys:
        rows = await _workspace_blueprint_document_rows(
            db, entity_id=entity_id, workspace_id=workspace_id, document_key=key,
        )
        if len(rows) != 1 or rows[0].is_trashed:
            raise InstallError("Workflow Knowledge starter is missing or ambiguous in this Workspace")
        await _require_blueprint_document_owned_by_workspace(db, rows[0], workspace_id)
        ids[key] = rows[0].id
    return WorkflowDocumentDependencyFactory.to_runtime(node, document_id_by_key=ids)


async def _materialize_workflow_skill_refs(
    db: AsyncSession, *, entity_id: str, workspace_id: Optional[str],
    user_id: Optional[str], node: Any, allow_install_external: bool = True,
) -> Any:
    """Only this installed Workspace's components may satisfy embedded refs."""
    node = await _materialize_workflow_document_refs(
        db, entity_id=entity_id, workspace_id=workspace_id, node=node,
    )
    if not WorkflowSkillDependencyFactory.has_portable_refs(node):
        return deepcopy(node)
    component_ids = {}
    if workspace_id:
        skills = list((await db.execute(select(Skill).where(
            Skill.entity_id == entity_id, Skill.workspace_id == workspace_id,
            Skill.status == "active",
        ))).scalars().all())
        for skill in skills:
            key = str((skill.config or {}).get("source_blueprint_component_key") or "").strip()
            if key:
                if key in component_ids:
                    raise InstallError(f"Blueprint Skill component {key!r} is ambiguous")
                component_ids[key] = skill.id
        from packages.core.models.marketplace_resource_link import MarketplaceResourceLink

        links = list((await db.execute(select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.relationship == "installed_component",
            MarketplaceResourceLink.scope_type == "workspace",
            MarketplaceResourceLink.scope_id == workspace_id,
            MarketplaceResourceLink.local_resource_type == RESOURCE_SKILL,
        ))).scalars().all())
        active_ids = {skill.id for skill in skills}
        for link in links:
            if link.component_key and link.local_resource_id in active_ids:
                previous = component_ids.setdefault(link.component_key, link.local_resource_id)
                if previous != link.local_resource_id:
                    raise InstallError(f"Blueprint Skill component {link.component_key!r} is ambiguous")
    resolved_ids = {}

    async def materialize(value):
        if isinstance(value, list):
            return [await materialize(item) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {key: await materialize(item) for key, item in value.items() if key != "skill_refs"}
        refs = value.get("skill_refs", [])
        if not isinstance(refs, list):
            raise InstallError("Workflow Skill references must name skill or skill_id arguments")
        for ref in refs:
            if not isinstance(ref, dict):
                raise InstallError("Workflow Skill reference must be an object")
            field, target = ref.get("field"), ref.get("target")
            if (field not in WorkflowSkillDependencyFactory.FIELDS
                or not isinstance(target, dict) or field in result):
                raise InstallError("Workflow Skill reference is invalid or conflicts with a runtime argument")
            if (not target.get("skill_marketplace_id")
                and target.get("skill_component_key") not in component_ids):
                raise InstallError("Workflow Skill component is missing from this installed Workspace")
            key = json.dumps(target, sort_keys=True)
            if key not in resolved_ids:
                if target.get("skill_marketplace_id") and not allow_install_external:
                    from packages.core.models.marketplace_resource_link import MarketplaceResourceLink

                    candidates = list((await db.execute(select(Skill).join(
                        MarketplaceResourceLink,
                        MarketplaceResourceLink.local_resource_id == Skill.id,
                    ).where(
                        Skill.entity_id == entity_id, Skill.status == "active",
                        MarketplaceResourceLink.entity_id == entity_id,
                        MarketplaceResourceLink.relationship == "installed_from",
                        MarketplaceResourceLink.local_resource_type == RESOURCE_SKILL,
                        MarketplaceResourceLink.marketplace_resource_id == target["skill_marketplace_id"],
                        MarketplaceResourceLink.marketplace_source == target.get("skill_marketplace_source", "platform"),
                    ))).scalars().unique().all())
                    if len(candidates) != 1:
                        raise InstallError("Workflow external Skill must already be installed before upgrading")
                    skill = candidates[0]
                else:
                    skill = await _resolve_blueprint_skill_target(
                        db, entity_id=entity_id, user_id=user_id, target=target,
                        skill_id_by_component_key=component_ids,
                    )
                resolved_ids[key] = skill.id
            result[field] = resolved_ids[key]
        return result

    return await materialize(node)


async def _install_workflow(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: Optional[str] = None,
    w: dict[str, Any],
    source_template_id: str,
    source_version: str = "1.0.0",
    installed_by: Optional[str] = None,
    adopt_unmapped: bool = True,
    authorize_existing_update: Optional[
        Callable[[WorkflowDefinition], Awaitable[None]]
    ] = None,
) -> Optional[str]:
    """Translate a blueprint workflow into a WorkflowDefinition row.

    Source identity is ``(installation mapping id, component key)``. Blueprint
    callers pass a Workspace-derived mapping id and create a Workspace-scoped
    definition, while MarketplaceResourceLink retains the canonical Blueprint
    id. A one-time adoption path remains for non-Blueprint legacy callers.
    """
    slug = w.get("slug")
    if not slug:
        logger.warning("blueprint install: workflow missing slug, skipping")
        return None
    w = deepcopy(w)
    w["steps"] = await _materialize_workflow_skill_refs(
        db, entity_id=entity_id, workspace_id=workspace_id,
        user_id=installed_by, node=w.get("steps") or [],
    )
    values = _blueprint_workflow_definition_values(w)
    await lock_reusable_resource_payload_references(
        db,
        entity_id=entity_id,
        payload=values.get("steps") or [],
    )

    component_key = str(slug)
    installation = (await db.execute(
        select(WorkflowTemplateInstallation)
        .where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.template_id == source_template_id,
            WorkflowTemplateInstallation.component_key == component_key,
        )
        .with_for_update()
    )).scalar_one_or_none()
    existing = None
    if installation is not None:
        existing = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.id == installation.workflow_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if existing is None:
            await db.delete(installation)
            await db.flush()
            installation = None
        elif workspace_id and existing.workspace_id != workspace_id:
            raise InstallError(
                "Blueprint Flow mapping points outside the target Workspace"
            )

    if existing is None and adopt_unmapped:
        mapped_workflow_ids = select(WorkflowTemplateInstallation.workflow_id).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
        )
        # Adopt legacy rows created before source ids existed, but never steal
        # a same-name Flow already owned by another template/Blueprint.
        existing = (await db.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == slug,
                WorkflowDefinition.id.not_in(mapped_workflow_ids),
            ).with_for_update()
        )).scalars().first()

    if existing is not None:
        if authorize_existing_update is not None:
            await authorize_existing_update(existing)
        if installation is None:
            installation = WorkflowTemplateInstallation(
                id=generate_ulid(),
                entity_id=entity_id,
                template_id=source_template_id,
                component_key=component_key,
                workflow_id=existing.id,
                installed_version=source_version,
                installed_by=installed_by,
                source_type="workspace_blueprint",
                installation_metadata={
                    "source_workflow_key": component_key,
                    "internal": bool(w.get("internal")),
                },
            )
            db.add(installation)
        else:
            installation.installed_version = source_version
            installation.installation_metadata = {
                **dict(installation.installation_metadata or {}),
                "source_workflow_key": component_key,
                "internal": bool(w.get("internal")),
            }
        logger.info(
            "blueprint install: workflow %r already exists for %s, reconciling",
            slug,
            source_template_id,
        )
        for field, value in values.items():
            setattr(existing, field, value)
        await db.flush()
        return existing.id

    row = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        created_by=installed_by,
        workspace_id=workspace_id,
        visibility=(Visibility.WORKSPACE if workspace_id else Visibility.ENTITY),
        **values,
    )
    db.add(row)
    await db.flush()
    db.add(WorkflowTemplateInstallation(
        id=generate_ulid(),
        entity_id=entity_id,
        template_id=source_template_id,
        component_key=component_key,
        workflow_id=row.id,
        installed_version=source_version,
        installed_by=installed_by,
        source_type="workspace_blueprint",
        installation_metadata={
            "source_workflow_key": component_key,
            "internal": bool(w.get("internal")),
        },
    ))
    await db.flush()
    return row.id


def _blueprint_runtime_step_type(step: dict[str, Any]) -> str:
    raw = str(step.get("type") or step.get("kind") or "agent").strip()
    return _BLUEPRINT_WORKFLOW_KIND_TO_TYPE.get(raw, raw or "agent")


def _blueprint_runtime_step_config(
    step: dict[str, Any],
    step_type: str,
) -> dict[str, Any]:
    config = dict(step.get("config") or {})
    excluded = {
        "id",
        "type",
        "kind",
        "depends_on",
        "name",
        "config",
        "next",
        "true_next",
        "false_next",
        "meta",
        "position",
        "chat_projection",
    }
    for key, value in step.items():
        if key not in excluded:
            config.setdefault(key, value)

    if step_type == "wait" and str(step.get("kind") or "") == "hitl_approval":
        config.setdefault("wait_type", "approval")
        config.setdefault("message", step.get("name") or "Operator approval required")
        config.setdefault("requires_operator_approval", True)
    return config


def _workflow_targets(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


async def _install_workflow_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    workflow_id: str,
    w: dict[str, Any],
    source_template_id: Optional[str] = None,
) -> Optional[str]:
    """Attach a blueprint workflow definition to the installed workspace."""
    slug = w.get("slug")
    if not slug:
        return None

    trigger_type = w.get("trigger_type") or "manual"
    if trigger_type == "schedule":
        logger.warning(
            "blueprint install: workflow %r uses trigger_type=schedule; "
            "scheduled work should be declared in recipe.scheduled_jobs",
            slug,
        )
        return None

    from packages.core.services import workflow_service

    binding_config = {
        **dict(w.get("binding_config") or {}),
        "source": "blueprint",
        "source_template_id": source_template_id,
        "workspace_blueprint_workflow_slug": slug,
    }
    if isinstance(w.get("proposal_authorization"), dict):
        binding_config["proposal_authorization"] = dict(
            w["proposal_authorization"]
        )
    variables_dict: dict[str, Any] = {}
    for v in w.get("variables") or []:
        if isinstance(v, dict) and v.get("key"):
            variables_dict[v["key"]] = v.get("default")
    deprecated_variable_keys = {
        str(key).strip()
        for key in w.get("deprecated_variable_keys") or []
        if str(key or "").strip()
    }
    trigger_config = _blueprint_workflow_trigger_config(w)
    binding_enabled = bool(w.get("enabled", True))
    binding_status = str(w.get("status") or "active")

    existing = await workflow_service.list_bindings(
        db,
        entity_id,
        workspace_id=workspace_id,
        workflow_id=workflow_id,
    )
    blueprint_bindings = [
        binding
        for binding in existing
        if (
            dict(binding.config or {}).get("source") == "blueprint"
            and dict(binding.config or {}).get("source_template_id")
            == source_template_id
            and dict(binding.config or {}).get("workspace_blueprint_workflow_slug")
            == slug
        )
    ]
    binding = next(
        (
            item for item in blueprint_bindings
            if item.trigger_type == trigger_type
        ),
        blueprint_bindings[0] if blueprint_bindings else None,
    )
    if binding is not None:
        binding.trigger_type = trigger_type
        binding.trigger_config = trigger_config
        binding.enabled = binding_enabled
        binding.status = binding_status
        binding.name = w.get("name") or slug
        binding.config = {
            **dict(binding.config or {}),
            **binding_config,
        }
        binding.variables = {
            **{
                key: value
                for key, value in dict(binding.variables or {}).items()
                if key not in deprecated_variable_keys
            },
            **variables_dict,
        }
        # Older installers could leave both the previous trigger binding and
        # the replacement behind. Preserve run history but retire duplicate
        # blueprint deployments so Agent discovery stays unambiguous.
        for duplicate in blueprint_bindings:
            if duplicate.id == binding.id:
                continue
            duplicate.enabled = False
            duplicate.status = "inactive"
        await db.flush()
        return binding.id

    binding = await workflow_service.create_workflow_binding(
        db,
        entity_id,
        workflow_id,
        workspace_id=workspace_id,
        name=w.get("name") or slug,
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        variables=variables_dict,
        config=binding_config,
    )
    binding.enabled = binding_enabled
    binding.status = binding_status
    await db.flush()
    return binding.id


# ── Post-install checks ───────────────────────────────────────────────


async def _run_post_install_check(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    check: dict[str, Any],
    todos: list[InstallTodo],
) -> None:
    """Execute one post_install_check inline. Failures become blocking
    todos so the operator knows the workspace isn't fully wired.

    Supported kinds (extend as new ones land in the schema):

      session_alive    — verify an IntegrationSession with provider /
                         (optional) label exists and status='active'
      agent_callable   — verify an AgentSubscription with service_key
                         exists and status='active'
      cron_scheduled   — verify the exact Workspace-scoped ScheduledJob id
                         derived from the blueprint's job_id
      workflow_present — verify the Workspace binding's stable Blueprint
                         workflow slug and its active definition (a real
                         ``workflow_dryrun`` invocation is a runtime concern,
                         deferred)
      blocking_setup_ready — evaluate the Workspace's declarative
                         ``settings.blocking_setup`` gate. Integration
                         failures already represented by a
                         ``missing_integration`` todo are de-duplicated.

    Unknown check kinds are recorded as a non-blocking note so the
    operator at least sees them.
    """
    if not isinstance(check, dict):
        return
    kind = check.get("kind")

    if kind == "session_alive":
        label = check.get("session_label")
        provider = check.get("provider")
        stmt = select(IntegrationSession).where(
            IntegrationSession.entity_id == entity_id,
            IntegrationSession.status == "active",
        )
        if provider:
            stmt = stmt.where(IntegrationSession.provider == provider)
        if label:
            stmt = stmt.where(IntegrationSession.label == label)
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is None:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    f"Post-install check failed: no active session "
                    f"(provider={provider!r}, label={label!r})."
                ),
                payload={"check": check, "result": "missing_session"},
                blocking=True,
            ))
        return

    if kind == "agent_callable":
        service_key = check.get("service_key")
        if not service_key:
            return
        row = (await db.execute(
            select(AgentSubscription.id)
            .join(Agent, Agent.id == AgentSubscription.agent_id)
            .where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.entity_id == entity_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
                Agent.entity_id == entity_id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    f"Post-install check failed: no active subscription "
                    f"with service_key={service_key!r}."
                ),
                payload={"check": check, "result": "missing_subscription"},
                blocking=True,
            ))
        return

    if kind == "cron_scheduled":
        job_id = check.get("job_id")
        if not job_id:
            return
        scoped_job_id = installed_blueprint_job_id(job_id, workspace_id)
        # The check proves the schedule is installed. Paused (by a person, or
        # after failing runs) it is still installed and must not block work.
        rows = list((await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.workspace_id == workspace_id,
                ScheduledJob.job_id == scoped_job_id,
            )
        )).scalars().all())
        if not rows:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    f"Post-install check failed: no scheduled job with id "
                    f"{scoped_job_id!r}."
                ),
                payload={"check": check, "result": "missing_scheduled_job"},
                blocking=True,
            ))
        return

    if kind in ("workflow_present", "workflow_dryrun"):
        slug = check.get("workflow_slug")
        if not slug:
            return
        bindings = (await db.execute(
            select(WorkflowBinding)
            .join(
                WorkflowDefinition,
                WorkflowDefinition.id == WorkflowBinding.workflow_id,
            )
            .where(
                WorkflowBinding.entity_id == entity_id,
                WorkflowBinding.workspace_id == workspace_id,
                WorkflowBinding.enabled.is_(True),
                WorkflowBinding.status == "active",
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.is_active.is_(True),
                WorkflowDefinition.status == "active",
            )
        )).scalars().all()
        workflow_id = next((
            binding.workflow_id
            for binding in bindings
            if dict(binding.config or {}).get(
                "workspace_blueprint_workflow_slug"
            ) == slug
        ), None)
        row = await db.get(WorkflowDefinition, workflow_id) if workflow_id else None
        if row is None:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
                detail=(
                    f"Post-install check failed: workflow {slug!r} not "
                    f"installed in this entity."
                ),
                payload={"check": check, "result": "missing_workflow"},
                blocking=True,
            ))
        return

    if kind == "blocking_setup_ready":
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None or workspace.entity_id != entity_id:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.BLOCKING_SETUP.value,
                detail="Blocking setup check failed: Workspace could not be loaded.",
                payload={"check": check, "result": "missing_workspace"},
                blocking=True,
            ))
            return

        from packages.core.services.workspace_readiness import (
            evaluate_workspace_blocking_setup,
        )

        configured_keys = {
            str(value or "").strip()
            for value in check.get("check_keys") or []
            if str(value or "").strip()
        }
        status = await evaluate_workspace_blocking_setup(
            db,
            workspace,
            check_keys=configured_keys or None,
        )
        if status is None:
            todos.append(InstallTodo(
                kind=BlueprintInstallTodoKind.BLOCKING_SETUP.value,
                detail=(
                    "Workspace blocking setup check is declared, but "
                    "settings.blocking_setup has no matching checks."
                ),
                payload={"check": check, "result": "blocking_setup_not_configured"},
                blocking=True,
            ))
            return
        if status.status == "ready":
            return

        incomplete = list(status.details.get("incomplete_checks") or [])
        covered_integration_providers = {
            str(todo.payload.get("provider") or todo.payload.get("server_slug") or "").strip()
            for todo in todos
            if todo.blocking
            and todo.kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value
        }
        uncovered = [
            result
            for result in incomplete
            if not (
                result.get("kind") == "integration_provider"
                and str(result.get("provider") or "").strip() in covered_integration_providers
            )
        ]
        if not uncovered:
            return
        keys = [str(result.get("key") or "setup") for result in uncovered]
        todos.append(InstallTodo(
            kind=BlueprintInstallTodoKind.BLOCKING_SETUP.value,
            detail=(
                "Workspace blocking setup is incomplete: "
                + ", ".join(keys)
                + ". Complete these prerequisites before normal Proposals or Flows run."
            ),
            payload={
                "check": check,
                "result": "blocking_setup_incomplete",
                "incomplete_checks": uncovered,
                "allowed_setup_task_keys": status.details.get("allowed_setup_task_keys") or [],
            },
            blocking=True,
        ))
        return

    # Unknown kind — surface as a note (non-blocking) so the operator
    # at least sees it and a future Manor can plug it in.
    todos.append(InstallTodo(
        kind=BlueprintInstallTodoKind.POST_INSTALL_CHECK.value,
        detail=f"Unknown post_install_check kind={kind!r}; skipping.",
        payload={"check": check, "result": "unknown_kind"},
        blocking=False,
    ))
