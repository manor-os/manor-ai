from __future__ import annotations

from collections.abc import Callable, Iterable
import copy
from dataclasses import dataclass

from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE

LOW_LEVEL_FILE_GENERATION_TOOLS = frozenset({
    "generate_image",
    "generate_video",
    "generate_document_file",
})

_AGENT_EAGER_CAPABILITY_TOOLS = {
    "runtime.discovery": ("search_tools",),
}

_MASTER_EAGER_CAPABILITY_TOOLS = {
    **_AGENT_EAGER_CAPABILITY_TOOLS,
    "web.safe_search": ("web_search", "web_fetch"),
    "skill.invoke": ("invoke_skill",),
    "workspace.search": ("rag",),
    "file.read": ("read_file", "list_files", "glob_files", "grep_files"),
    "file.write": ("write_file", "edit_file", "generate_file"),
    "sandbox.execute": (
        "sandbox_create",
        "sandbox_exec",
        "sandbox_read_file",
        "sandbox_write_file",
        "sandbox_save_result",
        "sandbox_destroy",
    ),
    "manor.composite": ("manor",),
    "cli.execute": ("bash",),
}

_WORKSPACE_AGENT_EAGER_CAPABILITY_TOOLS = {
    **_AGENT_EAGER_CAPABILITY_TOOLS,
    "workspace.operate": ("workspace_agent", "workspace_operation", "workspace_resolve_hitl", "workspace_create_task"),
    "workspace.search": ("workspace_search", "workspace_list_knowledge", "rag"),
    "workspace.task": ("workspace_create_task", "workspace_update_task_runtime", "workspace_agent"),
    "workspace.knowledge": (
        "workspace_create_knowledge_folder",
        "workspace_add_knowledge_documents",
        "workspace_remove_knowledge_document",
        "workspace_update_knowledge_policy",
    ),
    "workspace.governance": (
        "workspace_add_rule",
        "workspace_request_strategist_review",
        "workspace_operation",
    ),
}

_WORKSPACE_AGENT_CONTEXTUAL_CAPABILITY_TOOLS = {
    "manor.composite": ("manor",),
    "file.write": ("generate_file",),
    "skill.invoke": ("invoke_skill",),
    "web.safe_search": ("web_search", "web_fetch", "browse_web"),
    "file.read": ("read_file", "list_files", "glob_files", "grep_files"),
    "automation.manage": (
        "create_scheduled_job",
        "list_scheduled_jobs",
        "cancel_scheduled_job",
        "toggle_scheduled_job",
        "run_scheduled_job_now",
    ),
    "workflow.manage": (
        "list_workflows",
        "run_workflow",
        "list_workflow_definitions",
        "get_workflow",
        "create_workflow",
        "ai_edit_workflow",
        "update_workflow",
        "validate_workflow",
        "deploy_workflow",
        "delete_workflow",
        "test_workflow",
        "test_workflow_node",
        "list_workflow_runs",
        "get_workflow_run",
        "cancel_workflow_run",
        "resume_workflow_run",
        "import_workflow",
    ),
    "communication.notify": ("find_team_members", "notify_user"),
}

ALWAYS_LOADED = frozenset(
    tool_name
    for tool_names in _AGENT_EAGER_CAPABILITY_TOOLS.values()
    for tool_name in tool_names
)

def _flatten_capability_tool_subset(capability_tool_names: dict[str, tuple[str, ...]]) -> frozenset[str]:
    return frozenset(
        tool_name
        for tool_names in capability_tool_names.values()
        for tool_name in tool_names
    )


def _ordered_unique(*values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for seq in values:
        for value in seq:
            if value not in seen:
                seen.add(value)
                out.append(value)
    return tuple(out)


MASTER_ALWAYS_LOADED = _flatten_capability_tool_subset(_MASTER_EAGER_CAPABILITY_TOOLS)


def _validate_capability_tool_subset(capability_tool_names: dict[str, tuple[str, ...]]) -> None:
    for capability_id, tool_names in capability_tool_names.items():
        capability = CORE_CAPABILITIES.get(capability_id)
        if not capability:
            raise ValueError(f"Unknown runtime capability: {capability_id}")
        unknown = set(tool_names) - set(capability.tool_names)
        if unknown:
            raise ValueError(
                f"Tools {sorted(unknown)} are not declared by capability {capability_id}"
            )


_validate_capability_tool_subset(_AGENT_EAGER_CAPABILITY_TOOLS)
_validate_capability_tool_subset(_MASTER_EAGER_CAPABILITY_TOOLS)
_validate_capability_tool_subset(_WORKSPACE_AGENT_EAGER_CAPABILITY_TOOLS)
_validate_capability_tool_subset(_WORKSPACE_AGENT_CONTEXTUAL_CAPABILITY_TOOLS)


WORKSPACE_AGENT_ALWAYS_LOADED = _flatten_capability_tool_subset(_WORKSPACE_AGENT_EAGER_CAPABILITY_TOOLS)

WORKSPACE_AGENT_CONTEXTUAL_TOOLS = _flatten_capability_tool_subset(
    _WORKSPACE_AGENT_CONTEXTUAL_CAPABILITY_TOOLS
)


@dataclass(frozen=True)
class RuntimeToolSurfaceSpec:
    name: str
    eager_tool_names: frozenset[str]
    contextual_tool_names: frozenset[str] = frozenset()
    capability_ids: tuple[str, ...] = ()
    contextual_capability_ids: tuple[str, ...] = ()
    source: str = "runtime.tool_visibility"


@dataclass(frozen=True)
class RuntimeResolvedToolSurface:
    visible_tool_names: tuple[str, ...]
    eager_tool_names: frozenset[str]
    deferred_tool_names: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeToolRegistrySurface:
    prompt_schemas: tuple[dict, ...]
    visible_tool_names: tuple[str, ...]
    deferred_tool_names: tuple[str, ...]


def is_workspace_agent_tool_profile(tool_profile: str | None) -> bool:
    return tool_profile == WORKSPACE_AGENT_TOOL_PROFILE


def runtime_tool_surface_spec(
    *,
    is_master: bool,
    tool_profile: str | None = None,
) -> RuntimeToolSurfaceSpec:
    if is_workspace_agent_tool_profile(tool_profile):
        if is_master:
            return RuntimeToolSurfaceSpec(
                name=WORKSPACE_AGENT_TOOL_PROFILE,
                eager_tool_names=MASTER_ALWAYS_LOADED | WORKSPACE_AGENT_ALWAYS_LOADED,
                contextual_tool_names=WORKSPACE_AGENT_CONTEXTUAL_TOOLS,
                capability_ids=_ordered_unique(
                    _MASTER_EAGER_CAPABILITY_TOOLS.keys(),
                    _WORKSPACE_AGENT_EAGER_CAPABILITY_TOOLS.keys(),
                ),
                contextual_capability_ids=tuple(_WORKSPACE_AGENT_CONTEXTUAL_CAPABILITY_TOOLS.keys()),
            )
        return RuntimeToolSurfaceSpec(
            name=WORKSPACE_AGENT_TOOL_PROFILE,
            eager_tool_names=WORKSPACE_AGENT_ALWAYS_LOADED,
            contextual_tool_names=WORKSPACE_AGENT_CONTEXTUAL_TOOLS,
            capability_ids=tuple(_WORKSPACE_AGENT_EAGER_CAPABILITY_TOOLS.keys()),
            contextual_capability_ids=tuple(_WORKSPACE_AGENT_CONTEXTUAL_CAPABILITY_TOOLS.keys()),
        )
    if is_master:
        return RuntimeToolSurfaceSpec(
            name="master",
            eager_tool_names=MASTER_ALWAYS_LOADED,
            capability_ids=tuple(_MASTER_EAGER_CAPABILITY_TOOLS.keys()),
        )
    return RuntimeToolSurfaceSpec(
        name="agent",
        eager_tool_names=ALWAYS_LOADED,
        capability_ids=tuple(_AGENT_EAGER_CAPABILITY_TOOLS.keys()),
    )


def eager_tool_names_for_profile(
    *,
    is_master: bool,
    tool_profile: str | None = None,
) -> frozenset[str]:
    return runtime_tool_surface_spec(
        is_master=is_master,
        tool_profile=tool_profile,
    ).eager_tool_names


def runtime_tool_is_eager_for_profile(
    name: str,
    *,
    is_master: bool,
    tool_profile: str | None = None,
) -> bool:
    """Return whether a tool is eager for the resolved runtime tool surface."""

    clean = str(name or "").strip()
    if not clean:
        return False
    return clean in eager_tool_names_for_profile(
        is_master=is_master,
        tool_profile=tool_profile,
    )


_WORKSPACE_RUNTIME_TOOL_LABELS = {
    "rag": "Knowledge Search",
    "manor": "Manor Action Gateway",
    "workspace_agent": "Workspace Agent",
}


def _workspace_runtime_tool_descriptor(name: str, *, scope: str) -> dict[str, str]:
    return {
        "name": name,
        "display_name": _WORKSPACE_RUNTIME_TOOL_LABELS.get(name, name.replace("_", " ").title()),
        "category": "workspace_runtime",
        "scope": scope,
    }


def runtime_workspace_capability_tool_groups() -> dict[str, tuple[dict[str, str], ...]]:
    """Return workspace Runtime tools exposed by the capabilities API."""

    surface = runtime_tool_surface_spec(
        is_master=True,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )
    return {
        "always": tuple(
            _workspace_runtime_tool_descriptor(name, scope="always")
            for name in sorted(surface.eager_tool_names - {"search_tools"})
        ),
        "contextual": tuple(
            _workspace_runtime_tool_descriptor(name, scope="contextual")
            for name in sorted(surface.contextual_tool_names)
        ),
    }


def runtime_tool_auto_pass_names(
    *,
    is_master: bool,
    tool_profile: str | None = None,
    registered_tool_names: Iterable[str] | None = None,
) -> frozenset[str]:
    eager = set(
        eager_tool_names_for_profile(
            is_master=is_master,
            tool_profile=tool_profile,
        )
    )
    registered = set(registered_tool_names or ())
    if is_master and registered == {"code"}:
        # Lightweight unit-test / embedded pools may register only the
        # composite code tool. Expose it there without making code part of the
        # production master eager surface.
        eager.add("code")
    return frozenset(eager)


def runtime_tool_is_deferred(
    name: str,
    *,
    auto_pass_tool_names: set[str] | frozenset[str] | None = None,
) -> bool:
    """Return whether a registered tool schema should be deferred by default."""

    effective = auto_pass_tool_names if auto_pass_tool_names is not None else MASTER_ALWAYS_LOADED
    return name not in effective


def runtime_search_always_loaded_tool_names() -> frozenset[str]:
    """Return eager baseline names that should stay visible in tool search."""

    return ALWAYS_LOADED


def runtime_shadowed_file_generation_tool(
    name: str,
    *,
    bound_tool_names: set[str] | None,
    available_tool_names: Iterable[str] | None = None,
) -> bool:
    """Hide duplicate low-level generators when the composite gateway is usable."""

    if name not in LOW_LEVEL_FILE_GENERATION_TOOLS:
        return False
    if available_tool_names is not None and "generate_file" not in set(available_tool_names):
        return False
    return bound_tool_names is None or "generate_file" in bound_tool_names


def runtime_tool_visible_for_profile(
    name: str,
    *,
    bound_tool_names: set[str] | None,
    is_master: bool,
    mcp_allowed_names: set[str] | None,
    tool_profile: str | None = None,
    eager_tool_names: set[str] | frozenset[str] | None = None,
) -> bool:
    workspace_profile = is_workspace_agent_tool_profile(tool_profile)
    effective_master = is_master
    eager = set(
        eager_tool_names
        if eager_tool_names is not None
        else eager_tool_names_for_profile(
            is_master=is_master,
            tool_profile=tool_profile,
        )
    )

    # Master sees the same MCP surface in workspace chats as in global chats.
    # Non-master agents still need explicit workspace/agent MCP scope.
    if name.startswith("mcp__"):
        if mcp_allowed_names is None:
            return effective_master
        return name in mcp_allowed_names

    if workspace_profile:
        if effective_master:
            return True
        if name in eager:
            return True
        if bound_tool_names is not None and name in bound_tool_names:
            return True
        return name in WORKSPACE_AGENT_CONTEXTUAL_TOOLS

    if not effective_master and bound_tool_names is not None:
        if name not in eager and name not in bound_tool_names:
            return False
    return True


def resolve_runtime_tool_surface(
    registered_tool_names: Iterable[str],
    *,
    bound_tool_names: set[str] | None,
    is_master: bool,
    mcp_allowed_names: set[str] | None,
    tool_profile: str | None = None,
) -> RuntimeResolvedToolSurface:
    registered = tuple(name for name in registered_tool_names if name)
    eager = set(runtime_tool_auto_pass_names(
        is_master=is_master,
        tool_profile=tool_profile,
        registered_tool_names=registered,
    ))
    # A skill-bound agent needs the invocation schema in its first round;
    # otherwise it can describe bound skills but cannot call any of them.
    if bound_tool_names is not None and "invoke_skill" in bound_tool_names:
        eager.add("invoke_skill")
    available = set(registered)
    visible: list[str] = []
    deferred: list[str] = []
    for name in registered:
        if runtime_shadowed_file_generation_tool(
            name,
            bound_tool_names=bound_tool_names,
            available_tool_names=available,
        ):
            continue
        if not runtime_tool_visible_for_profile(
            name,
            bound_tool_names=bound_tool_names,
            is_master=is_master,
            mcp_allowed_names=mcp_allowed_names,
            tool_profile=tool_profile,
            eager_tool_names=eager,
        ):
            continue
        visible.append(name)
        if name not in eager:
            deferred.append(name)
    return RuntimeResolvedToolSurface(
        visible_tool_names=tuple(visible),
        eager_tool_names=frozenset(eager),
        deferred_tool_names=tuple(deferred),
    )


def runtime_tool_schemas_for_resolved_surface(
    resolved_surface: RuntimeResolvedToolSurface,
    *,
    tool_schema_resolver: Callable[[str], dict | None],
) -> tuple[list[dict], list[str]]:
    """Materialize prompt-visible schemas for an already resolved tool surface."""

    deferred_names = list(resolved_surface.deferred_tool_names)
    deferred_set = set(deferred_names)
    schemas: list[dict] = []
    for name in resolved_surface.visible_tool_names:
        if name in deferred_set:
            continue
        schema = tool_schema_resolver(name)
        if schema is not None:
            schemas.append(copy.deepcopy(schema))

    from packages.core.ai.runtime.tool_discovery import (
        runtime_apply_deferred_tool_discovery_hint,
    )

    runtime_apply_deferred_tool_discovery_hint(schemas, deferred_names)
    return schemas, deferred_names


def runtime_tool_registry_surface_from_schemas(
    tool_schemas: Iterable[tuple[str, dict]],
    *,
    bound_tool_names: set[str] | None,
    is_master: bool,
    mcp_allowed_names: set[str] | None,
    tool_profile: str | None = None,
) -> RuntimeToolRegistrySurface:
    """Resolve a prompt-visible tool surface from a registry schema snapshot."""

    schema_map = {
        str(name): schema
        for name, schema in tool_schemas
        if str(name or "").strip()
    }
    resolved_surface = resolve_runtime_tool_surface(
        schema_map.keys(),
        bound_tool_names=bound_tool_names,
        is_master=is_master,
        mcp_allowed_names=mcp_allowed_names,
        tool_profile=tool_profile,
    )
    prompt_schemas, deferred_names = runtime_tool_schemas_for_resolved_surface(
        resolved_surface,
        tool_schema_resolver=schema_map.get,
    )
    return RuntimeToolRegistrySurface(
        prompt_schemas=tuple(prompt_schemas),
        visible_tool_names=tuple(resolved_surface.visible_tool_names),
        deferred_tool_names=tuple(deferred_names),
    )


def runtime_search_bound_tool_names_for_profile(
    available_tool_names: Iterable[str],
    *,
    tool_profile: str | None = None,
    context_allowed_tool_names: set[str] | None = None,
    bound_tool_names: set[str] | None = None,
    mcp_allowed_names: set[str] | None = None,
) -> set[str] | None:
    """Resolve the search_tools discovery surface for a runtime profile.

    ToolPool should not know that the workspace profile intentionally narrows
    search discovery even when Manor AI is acting as workspace master. Keeping
    that rule here makes profile-specific visibility a runtime concern only.
    """

    workspace_profile = is_workspace_agent_tool_profile(tool_profile)
    if not workspace_profile:
        return bound_tool_names
    if context_allowed_tool_names is not None:
        return set(context_allowed_tool_names)
    return set(
        resolve_runtime_tool_surface(
            available_tool_names,
            bound_tool_names=bound_tool_names,
            is_master=bound_tool_names is None,
            mcp_allowed_names=mcp_allowed_names,
            tool_profile=tool_profile,
        ).visible_tool_names
    )
