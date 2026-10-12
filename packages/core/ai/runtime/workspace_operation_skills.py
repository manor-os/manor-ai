"""Shared, versioned instruction guides for Workspace Chat operations.

Only repository-owned text and tool contracts enter these caches. Actor scope
is applied after loading; Workspace state, grants and approvals are never cached.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


WORKSPACE_OPERATION_GUIDANCE_START = "<workspace_operation_guides>"
WORKSPACE_OPERATION_GUIDANCE_END = "</workspace_operation_guides>"

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
WORKSPACE_OPERATION_SKILL_SLUGS = frozenset({
    "workspace-operations", "workspace-app", "workspace-configuration",
    "workspace-workflows", "workspace-automations", "workspace-tasks",
    "workspace-knowledge", "workspace-goals-handoffs",
})


@lru_cache(maxsize=32)
def _read_source(path: str, signature: tuple[int, int, int]) -> str:
    return Path(path).read_text(encoding="utf-8")


def _source(path: Path) -> str:
    stat = path.stat()
    return _read_source(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))


def _skill_source(slug: str) -> tuple[str, str]:
    if slug not in WORKSPACE_OPERATION_SKILL_SLUGS:
        raise ValueError(f"Unknown Workspace operation guide: {slug}")
    directory = _SKILLS_ROOT / slug
    return _source(directory / "SKILL.md"), _source(directory / "config.json")


def workspace_operation_tool_contract(config: Mapping[str, Any]) -> dict:
    """Project the real registry; hand-written guides do not create tools/actions."""
    from packages.core.ai.runtime.composite_tools import WorkspaceToolAction
    from packages.core.ai.runtime.tool_registry import runtime_tool_schema
    from packages.core.ai.runtime.workspace_operation_actions import WORKSPACE_OPERATION_ACTIONS
    from packages.core.services.workspace_operation_service import OPERATION_PATCH_OP_CATALOG
    from packages.core.ai.tools.manor_tool import manor_action_manifests

    schemas = {}
    for name in sorted(config.get("tools") or ()):
        schema = runtime_tool_schema(name)
        if schema is None:
            raise ValueError(f"Workspace operation guide references unregistered tool: {name}")
        schemas[name] = schema
    spec = config.get("workspace_operations") or {}
    actions = sorted(spec.get("workspace_actions") or ())
    unknown = set(actions) - {action.value for action in WorkspaceToolAction}
    if unknown:
        raise ValueError(f"Unknown Workspace actions: {sorted(unknown)}")
    catalog = {
        name: {"canonical": entry.canonical, "summary": entry.summary,
               **({"payload_schema": entry.payload_schema} if entry.payload_schema is not None else {})}
        for entry in OPERATION_PATCH_OP_CATALOG
        for name in (entry.canonical, *entry.aliases)
    }
    patches = sorted(spec.get("patch_ops") or ())
    unknown = set(patches) - catalog.keys()
    if unknown:
        raise ValueError(f"Unknown Workspace patch operations: {sorted(unknown)}")
    manifests = {item["invoke_with"]["arguments"]["action"]: item for item in manor_action_manifests()}
    manor_actions = sorted(spec.get("manor_actions") or ())
    unknown = set(manor_actions) - manifests.keys()
    if unknown:
        raise ValueError(f"Unknown Manor actions: {sorted(unknown)}")
    # The compatibility schema describes the nested operation params even
    # when internal masters expose only the Manor composite entrypoint.
    operation_parameters = None
    if "operation" in actions:
        operation_parameters = runtime_tool_schema("workspace_operation")["function"]["parameters"]
    return {
        "tools": schemas, "workspace_actions": actions,
        "manor_actions": {name: manifests[name] for name in manor_actions},
        "operation_parameters": operation_parameters,
        "operation_actions": list(WORKSPACE_OPERATION_ACTIONS) if "operation" in actions else [],
        "patch_ops": {name: catalog[name] for name in patches},
    }


def workspace_operation_tool_contract_digest(config: Mapping[str, Any]) -> str:
    serialized = json.dumps(workspace_operation_tool_contract(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _schema_constraints(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _schema_constraints(item) for key, item in value.items() if key != "description" or not isinstance(item, str)}
    if isinstance(value, list):
        return [_schema_constraints(item) for item in value]
    return value


def _operation_parameter_constraints(contract: dict) -> dict | None:
    parameters = _schema_constraints(contract["operation_parameters"])
    if parameters is None:
        return None
    # Project only this guide's canonical patches, preserving each registered
    # payload constraint. Full schemas remain in the fingerprint dependency.
    ops = set(contract["patch_ops"])
    items = parameters["properties"]["patches"]["items"]
    items["properties"]["op"]["enum"] = sorted(ops)
    branches = []
    for branch in items["oneOf"]:
        names = sorted(set(branch["properties"]["op"]["enum"]) & ops)
        if names:
            branch["properties"]["op"]["enum"] = names
            branches.append(branch)
    items["oneOf"] = branches
    return parameters


@lru_cache(maxsize=32)
def _guide(markdown: str, config_text: str, contract_text: str) -> tuple[str, str]:
    """Cache immutable public content by both source and live tool contract."""
    from packages.core.services.builtin_skill_loader import _parse_frontmatter

    frontmatter, body = _parse_frontmatter(markdown)
    config = json.loads(config_text)
    contract = json.loads(contract_text)
    digest = hashlib.sha256((markdown + "\0" + config_text + "\0" + contract_text).encode()).hexdigest()
    catalog = {
        "tools": list(contract["tools"]),
        "workspace_actions": contract["workspace_actions"],
        "manor_actions": contract["manor_actions"],
        "operation_parameters": _operation_parameter_constraints(contract),
        "operation_actions": contract["operation_actions"],
        "patch_ops": contract["patch_ops"],
    }
    rendered = (
        f"### Guide `{config['name']}` v{frontmatter.get('version', '1.0.0')} ({digest[:12]})\n"
        f"{body.strip()}\n\n"
        "Supported tool catalog (load deferred schemas with search_tools; scope is checked at execution):\n"
        + json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return rendered, digest


def load_workspace_operation_guide(slug: str) -> tuple[str, str]:
    markdown, config_text = _skill_source(slug)
    contract = workspace_operation_tool_contract(json.loads(config_text))
    return _guide(markdown, config_text, json.dumps(contract, sort_keys=True, separators=(",", ":")))


def _matches(text: str, terms: list[str]) -> bool:
    return any(
        term in text if not term.isascii() else re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)
        for term in terms
    )


def select_workspace_operation_guides(message: str | None, configs: Mapping[str, dict]) -> tuple[str, ...]:
    text = str(message or "").casefold()
    selected = tuple(
        slug for slug, config in configs.items()
        if _matches(text, (config.get("workspace_operations") or {}).get("route_terms") or [])
    )
    # Linking a Flow/Automation/Agent to the App is owned by App configuration.
    # The caller can load another guide if authoring/execution is also needed.
    if "workspace-app" in selected:
        return ("workspace-app",)
    return selected[:2]


def workspace_operation_guide_configs(allowed: set[str] | None = None) -> dict[str, dict]:
    _, root_config_text = _skill_source("workspace-operations")
    configs = {}
    for slug in json.loads(root_config_text)["workspace_operations"]["children"]:
        _, config_text = _skill_source(slug)
        config = json.loads(config_text)
        tools = set(config["tools"]) - {"search_tools", "invoke_skill"}
        if allowed is None or tools & allowed:
            configs[slug] = config
    return configs


def workspace_operation_guide_index(configs: Mapping[str, dict]) -> str:
    return "### Operation guide index\n" + "\n".join(
        f"- `{slug}`: {config['description']}" for slug, config in configs.items()
    )


def render_workspace_operation_skills(ctx: Any) -> str | None:
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    ctx.runtime_workspace_operation_skills = []
    ctx.runtime_workspace_operation_selected_guides = None
    envelope = getattr(ctx, "runtime_envelope", None)
    if envelope is not None:
        envelope.metadata.pop("workspace_operation_skills", None)
    if (
        envelope is None
        or envelope.surface != ChatSurface.WORKSPACE_CHAT
        or envelope.profile != RuntimeProfile.WORKSPACE_OPERATOR
        or not getattr(ctx, "workspace_id", None)
        or getattr(ctx, "task_id", None)
        or getattr(ctx, "manual_skill_selected", False)
    ):
        return None
    allowed = envelope.effective_allowed_tool_names()
    if not {"manor", "invoke_skill"}.issubset(allowed):
        return None

    configs = workspace_operation_guide_configs(allowed)
    root_guide, root_digest = load_workspace_operation_guide("workspace-operations")
    parts = [root_guide, workspace_operation_guide_index(configs)]
    selected: list[str] = []
    loaded = [{"slug": "workspace-operations", "fingerprint": root_digest}]
    for slug in select_workspace_operation_guides(getattr(ctx, "active_user_message", None), configs):
        guide, digest = load_workspace_operation_guide(slug)
        selected.append(guide)
        loaded.append({"slug": slug, "fingerprint": digest})
    ctx.runtime_workspace_operation_skills = loaded
    envelope.metadata["workspace_operation_skills"] = loaded
    if getattr(ctx, "prompt_split_turn_context", False):
        # The router and index are the same every turn; the guides matched to
        # this message travel with the turn context instead.
        if selected:
            ctx.runtime_workspace_operation_selected_guides = (
                "## Workspace operation guides for this request\n" + "\n\n".join(selected)
            )
    else:
        parts.extend(selected)
    return WORKSPACE_OPERATION_GUIDANCE_START + "\n" + "\n\n".join(parts) + "\n" + WORKSPACE_OPERATION_GUIDANCE_END
