from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.capabilities import capability_for_id, tool_names_for_capability_ids
from packages.core.ai.runtime.profiles import RuntimeProfile


_CAPABILITY_KEYS = (
    "capability_id",
    "runtime_capability_id",
    "business_capability_id",
    "business_capability",
    "runtime_capability",
    "capability",
    "capability_key",
    "tool_name",
    "skill_key",
    "integration_key",
    "channel_type",
    "name",
)

RUNTIME_CAPABILITY_BINDING_TYPES = {
    "action",
    "business_capability",
    "capability",
    "channel",
    "integration",
    "mcp",
    "runtime_capability",
    "skill",
    "tool",
}

# Known MCP integration server_keys (mirrors BUILTIN_MCP_MODULES in
# packages/core/ai/mcp/__init__.py). Kept as static data — NOT imported — so
# this core module doesn't pull the (partly OSS-excluded) MCP server modules at
# import time. Drift is safe: a brand-new integration simply isn't auto-recovered
# and falls through to an honest validation error.
_KNOWN_INTEGRATION_SERVER_KEYS = frozenset({
    "gmail", "google_calendar", "manor_mcp_calendar", "google_drive", "github",
    "linkedin", "quickbooks", "stripe", "twitter_x", "email", "wechat_official",
    "wechat_personal", "telegram", "replicate", "elevenlabs", "tavily", "jimeng",
    "claude_code", "codex_cli", "gemini_cli", "cursor_cli", "aider", "continue_cli",
    "producthunt", "facebook", "chrome", "chrome_knowledge_local", "knowledge_local",
    "local_browser", "youtube", "tiktok", "shopify", "woocommerce", "square",
    "tiktok_shop", "amazon",
})

# Well-known aliases models use for integrations that don't match a server_key
# verbatim. Conservative on purpose — only unambiguous mislabels.
_INTEGRATION_ALIASES = {
    "twitter": "twitter_x",
    "x": "twitter_x",
    "chrome_extension": "chrome",
    "browser_extension": "chrome",
}


def _resolve_integration_server_key(name: str) -> str | None:
    """Resolve a possibly-mislabeled capability name to a known MCP server_key.

    Returns the server_key when ``name`` clearly refers to a known integration
    (exact match, alias, or a token like ``chrome`` inside ``browser.chrome_extension``),
    else None so the caller leaves the binding for honest validation."""
    text = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not text:
        return None
    if text in _KNOWN_INTEGRATION_SERVER_KEYS:
        return text
    if text in _INTEGRATION_ALIASES:
        return _INTEGRATION_ALIASES[text]
    for token in text.split("_"):
        if token in _KNOWN_INTEGRATION_SERVER_KEYS:
            return token
        if token in _INTEGRATION_ALIASES:
            return _INTEGRATION_ALIASES[token]
    return None


def _exact_integration_server_key(name: str) -> str | None:
    """Like ``_resolve_integration_server_key`` but exact-only (no per-token
    fallback), so a genuine skill/name is never rewritten into an integration."""
    text = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not text:
        return None
    if text in _KNOWN_INTEGRATION_SERVER_KEYS:
        return text
    if text in _INTEGRATION_ALIASES:
        return _INTEGRATION_ALIASES[text]
    return None


_LEGACY_ACTION_CAPABILITY_TYPE_ALIASES = {
    # Legacy/LLM-friendly operation labels. These describe what the bound
    # capability does, not the binding transport, so treat them as action
    # bindings unless the row explicitly points at a tool.
    "automate": "action",
    "create": "action",
    "delete": "action",
    "email": "action",
    "message": "action",
    "post": "action",
    "publish": "action",
    "read": "action",
    "schedule": "action",
    "search": "action",
    "send": "action",
    "update": "action",
    "write": "action",
}

WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES = (
    "manor",
    "workspace_agent",
    "workspace_operation",
    "rag",
    "generate_file",
)


@dataclass(frozen=True)
class RuntimeCapabilityBindingExpansion:
    tool_names: set[str] = field(default_factory=set)
    mcp_tool_names: set[str] = field(default_factory=set)
    capability_ids: set[str] = field(default_factory=set)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _unique_clean_strings(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    for value in raw_values:
        if isinstance(value, dict):
            value = (
                value.get("capability_id")
                or value.get("id")
                or value.get("name")
                or value.get("tool_name")
            )
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def runtime_binding_capability_name(binding: dict[str, Any]) -> str:
    return str(next((binding.get(key) for key in _CAPABILITY_KEYS if binding.get(key)), "")).strip()


def runtime_binding_tool_name(binding: dict[str, Any]) -> str:
    return str(
        binding.get("tool_name")
        or binding.get("capability_key")
        or binding.get("name")
        or ""
    ).strip()


def runtime_binding_service_key(binding: dict[str, Any]) -> str:
    return str(
        binding.get("owner_service_key")
        or binding.get("service_key")
        or binding.get("owner_id")
        or ""
    ).strip()


def _normalize_capability_type(
    value: Any,
    default_type: str = "tool",
    *,
    binding: dict[str, Any] | None = None,
) -> str:
    raw = str(value or default_type or "tool").strip().lower()
    if raw == "publish" and isinstance(binding, dict) and str(binding.get("tool_name") or "").strip():
        return "tool"
    return _LEGACY_ACTION_CAPABILITY_TYPE_ALIASES.get(raw, raw)


def runtime_skill_binding_ref(binding: dict[str, Any]) -> str:
    return str(
        binding.get("skill_id")
        or binding.get("skill_key")
        or binding.get("capability_key")
        or binding.get("name")
        or ""
    ).strip()


def runtime_binding_owner_matches(
    binding: dict[str, Any],
    *,
    agent_id: str | None,
    is_master: bool,
    task_id: str | None = None,
    current_service_keys: set[str] | frozenset[str] | None = None,
    task_service_keys: set[str] | frozenset[str] | None = None,
    subscription_agent_ids_by_id: dict[str, str | None] | None = None,
) -> bool:
    owner_scope = str(binding.get("owner_scope") or "").strip()
    current_service_keys = set(current_service_keys or set())
    subscription_agent_ids_by_id = subscription_agent_ids_by_id or {}

    if owner_scope == "workspace_agent":
        return is_master

    if owner_scope == "service":
        service_key = runtime_binding_service_key(binding)
        if not service_key:
            return False
        if is_master and task_service_keys is not None:
            scoped_task_services = set(task_service_keys)
            return not scoped_task_services or service_key in scoped_task_services
        return service_key in current_service_keys

    if owner_scope == "agent":
        owner_agent_id = str(binding.get("agent_id") or binding.get("owner_id") or "").strip()
        owner_sub_id = str(binding.get("agent_subscription_id") or "").strip()
        if owner_agent_id and owner_agent_id == str(agent_id or ""):
            return True
        if owner_sub_id and owner_sub_id in subscription_agent_ids_by_id:
            return subscription_agent_ids_by_id[owner_sub_id] == agent_id
        return False

    if owner_scope == "task":
        owner_task_id = str(binding.get("task_id") or binding.get("owner_id") or "").strip()
        return bool(owner_task_id and owner_task_id == str(task_id or ""))

    return False


def runtime_capability_binding_identity(
    binding: dict[str, Any],
    *,
    default_type: str = "tool",
) -> str:
    if binding.get("binding_key"):
        return str(binding["binding_key"])
    capability = runtime_binding_capability_name(binding) or "capability"
    owner_scope = binding.get("owner_scope") or "unscoped"
    owner_id = (
        binding.get("owner_id")
        or binding.get("owner_service_key")
        or binding.get("service_key")
        or binding.get("agent_id")
        or binding.get("agent_subscription_id")
        or binding.get("task_id")
        or "workspace"
    )
    capability_type = _normalize_capability_type(
        binding.get("capability_type"),
        default_type=default_type,
        binding=binding,
    )
    return f"{capability_type}:{capability}:{owner_scope}:{owner_id}"


def normalize_runtime_capability_binding(
    raw_binding: dict[str, Any],
    *,
    default_type: str = "tool",
) -> dict[str, Any]:
    binding = dict(raw_binding)
    binding["capability_type"] = _normalize_capability_type(
        binding.get("capability_type"),
        default_type=default_type,
        binding=binding,
    )
    _recover_mislabeled_integration_binding(binding)
    binding.setdefault(
        "binding_key",
        runtime_capability_binding_identity(binding, default_type=default_type),
    )
    return binding


def _recover_mislabeled_integration_binding(binding: dict[str, Any]) -> None:
    """Rewrite an integration bound as a (nonexistent) capability to the mcp shape.

    Models sometimes bind an MCP integration — the Chrome extension being the
    canonical case — as ``capability_type='capability'`` with an invented
    ``capability_id`` like ``browser.chrome_extension`` instead of the correct
    ``capability_type='mcp', integration_key='chrome'``. When the declared
    capability isn't a real runtime capability but its name resolves to a known
    MCP server, coerce it to the mcp shape so the binding is valid and actually
    grants the integration. Also covers the ``capability_type='skill'`` variant
    (e.g. ``skill_key='chrome'``) — models mislabel the same integration as a
    skill. When it does NOT resolve, leave it untouched so validation surfaces
    the real 'unknown runtime capability' error rather than silently inventing a
    binding."""
    capability_type = binding.get("capability_type")
    name = runtime_binding_capability_name(binding)
    if not name:
        return
    if capability_type in {"capability", "business_capability", "runtime_capability"}:
        # An id that isn't a real capability but names a known integration —
        # token match is fine here ('browser.chrome_extension' -> chrome).
        if capability_for_id(name) is not None:
            return
        server_key = _resolve_integration_server_key(name)
    elif capability_type == "skill":
        # A skill binding whose key is EXACTLY a known integration server is a
        # mislabeled integration (e.g. skill_key='chrome'); no genuine skill is
        # named after a server_key. Exact-only, so a real skill is never
        # rewritten into an integration.
        server_key = _exact_integration_server_key(name)
    else:
        return
    if not server_key:
        return
    for key in (
        "capability_id",
        "runtime_capability_id",
        "business_capability_id",
        "business_capability",
        "runtime_capability",
        "capability",
        "skill_key",
    ):
        binding.pop(key, None)
    binding["capability_type"] = "mcp"
    binding["integration_key"] = server_key
    binding.pop("binding_key", None)  # re-derive from the resolved integration


def validate_runtime_capability_binding(
    raw_binding: dict[str, Any],
    *,
    path: str,
    service_keys: set[str] | None = None,
    valid_owner_scopes: set[str] | None = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    service_keys = service_keys or set()
    valid_owner_scopes = valid_owner_scopes or {"workspace_agent", "service", "agent", "task"}

    if raw_binding.get("enabled") is False:
        return errors

    capability = runtime_binding_capability_name(raw_binding)
    if not capability:
        errors.append({"path": path, "message": "binding requires capability key/name"})

    capability_type = _normalize_capability_type(
        raw_binding.get("capability_type"),
        binding=raw_binding,
    )
    if capability_type not in RUNTIME_CAPABILITY_BINDING_TYPES:
        errors.append({
            "path": f"{path}.capability_type",
            "message": f"unknown runtime capability binding type {capability_type!r}",
        })

    if capability_type in {"capability", "business_capability", "runtime_capability"}:
        if capability_for_id(capability) is None:
            errors.append({
                "path": f"{path}.capability_id",
                "message": f"unknown runtime capability {capability!r}",
            })

    if capability_type in {"mcp", "integration"} and not capability.startswith("mcp__"):
        allowed_tools = _as_list(
            raw_binding.get("allowed_tools")
            or raw_binding.get("mcp_allowed_tools")
        )
        if allowed_tools and any(not str(tool or "").strip() for tool in allowed_tools):
            errors.append({
                "path": f"{path}.allowed_tools",
                "message": "allowed_tools entries must be non-empty tool names",
            })

    owner_scope = str(raw_binding.get("owner_scope") or "").strip()
    if owner_scope not in valid_owner_scopes:
        errors.append({
            "path": f"{path}.owner_scope",
            "message": "binding must be owned by workspace_agent, service, agent, or task",
        })
        return errors

    if owner_scope == "service":
        service_key = runtime_binding_service_key(raw_binding)
        if not service_key:
            errors.append({
                "path": f"{path}.owner_service_key",
                "message": "service-owned binding requires service key",
            })
        elif service_keys and service_key not in service_keys:
            errors.append({
                "path": f"{path}.owner_service_key",
                "message": f"unknown service {service_key!r}",
            })

    if owner_scope == "agent" and not (
        raw_binding.get("agent_id")
        or raw_binding.get("agent_subscription_id")
        or raw_binding.get("owner_id")
    ):
        errors.append({
            "path": f"{path}.agent_id",
            "message": "agent-owned binding requires agent id/subscription id",
        })

    if owner_scope == "task" and not (raw_binding.get("task_id") or raw_binding.get("owner_id")):
        errors.append({
            "path": f"{path}.task_id",
            "message": "task-owned binding requires task id",
        })

    return errors


def expand_runtime_capability_binding(
    binding: dict[str, Any],
    *,
    profile: RuntimeProfile = RuntimeProfile.WORKSPACE_OPERATOR,
) -> RuntimeCapabilityBindingExpansion:
    capability_type = _normalize_capability_type(
        binding.get("capability_type"),
        binding=binding,
    )
    capability = runtime_binding_capability_name(binding)
    out = RuntimeCapabilityBindingExpansion()

    if capability_type in {"capability", "business_capability", "runtime_capability"}:
        expanded_tools = tool_names_for_capability_ids({capability}, profile=profile)
        if expanded_tools:
            out.tool_names.update(expanded_tools)
            out.capability_ids.add(capability)
            return out

    if capability_for_id(capability) is not None:
        expanded_tools = tool_names_for_capability_ids({capability}, profile=profile)
        if expanded_tools:
            out.tool_names.update(expanded_tools)
            out.capability_ids.add(capability)
            return out

    tool_name = runtime_binding_tool_name(binding)
    if capability.startswith("mcp__"):
        out.mcp_tool_names.add(capability)
        return out
    if tool_name.startswith("mcp__"):
        out.mcp_tool_names.add(tool_name)
        return out

    if capability_type in {"tool", "action", "capability"} and tool_name:
        out.tool_names.add(tool_name)
        return out

    if capability_type in {"mcp", "integration"}:
        server_key = str(
            binding.get("mcp_server_key")
            or binding.get("integration_key")
            or capability
            or ""
        ).strip()
        allowed = _as_list(binding.get("allowed_tools") or binding.get("mcp_allowed_tools"))
        if not allowed and server_key:
            try:
                from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names

                out.mcp_tool_names.update(
                    runtime_registered_tool_names(prefix=f"mcp__{server_key}__")
                )
            except Exception:
                pass
            return out
        for tool in allowed:
            mcp_tool = str(tool or "").strip()
            if not mcp_tool:
                continue
            out.mcp_tool_names.add(
                mcp_tool if mcp_tool.startswith("mcp__") else f"mcp__{server_key}__{mcp_tool}"
            )
    return out


def normalize_workspace_custom_agent_tool_bindings(
    requested_tool_names: Any,
    *,
    business_capability_ids: Any = (),
    has_skills: bool = False,
    profile: RuntimeProfile = RuntimeProfile.WORKSPACE_OPERATOR,
) -> tuple[str, ...]:
    """Normalize tool bindings for agents created from workspace setup.

    This keeps legacy ``tool_bindings`` working while allowing architects to
    specify capability ids. Baseline tools stay intentionally narrow to avoid
    silently widening old custom-agent behavior.
    """

    merged: list[str] = []
    seen: set[str] = set()

    def add_many(names: Any) -> None:
        for name in _unique_clean_strings(names):
            if name not in seen:
                seen.add(name)
                merged.append(name)

    add_many(requested_tool_names)
    for capability_id in _unique_clean_strings(business_capability_ids):
        capability = capability_for_id(capability_id)
        if capability is None:
            continue
        add_many(tool_names_for_capability_ids({capability_id}, profile=profile))

    add_many(WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES)
    if has_skills:
        add_many(("invoke_skill",))
    return tuple(merged)
