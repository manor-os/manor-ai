"""Translate App presentation references at the Blueprint boundary.

App configuration contains no capabilities of its own. Every runner must resolve
against the Workspace components exported or installed in the same transaction.
"""

from __future__ import annotations

import re
from typing import Any

from packages.core.services.workspace_app_view import (
    app_target_kind_and_id,
    normalize_app_view_config,
)

#: Folders of ``apps/web/public/blueprints`` — pictures and loops the platform
#: ships with every web build, so the same path works on any deployment of it.
PLATFORM_ASSET_DIRS = ("covers", "creative", "motion", "recording", "styles")
_PLATFORM_ASSET_PATH = re.compile(
    rf"/blueprints/(?:{'|'.join(PLATFORM_ASSET_DIRS)})(?:/[a-z0-9][a-z0-9_-]*)+\.(?:webp|png|jpg|mp4|webm)"
    r"(?:\?v=[A-Za-z0-9._-]{1,32})?"  # a cache-busting revision, as the web app's own links carry
)


def is_platform_asset_path(value: str) -> bool:
    """A file the platform itself serves, as opposed to one deployment's upload."""
    return bool(_PLATFORM_ASSET_PATH.fullmatch(value))


def is_portable_asset_url(value: str) -> bool:
    """An address a Blueprint may name: https, a platform file, a file its
    package carries, or where installing a package put one (``blueprints.package``)."""
    from packages.core.blueprints.package import is_installed_asset_url, is_package_asset_path

    return (value.startswith("https://") or is_platform_asset_path(value)
            or is_package_asset_path(value) or is_installed_asset_url(value))


def remap_app_view(
    value: Any,
    *,
    workflows: dict[str, str],
    agents: dict[str, str],
    automations: dict[str, str],
    workflow_definitions: dict[str, str] | None = None,
    stat_keys: set[str] | None = None,
) -> dict[str, Any]:
    config = normalize_app_view_config(value)
    maps = {"workflow": workflows, "agent": agents, "automation": automations,
            "workflow_definition": workflow_definitions or {}}

    def reference(kind: str, identity: str) -> str:
        if kind == "agent" and identity == "default":
            return "agent:default"
        resolved = maps[kind].get(identity)
        if not resolved:
            raise ValueError(f"App {kind} target {identity!r} is not included in this Blueprint")
        return f"{kind}:{resolved}"

    def action(key: str) -> str:
        kind, identity = key.split(":", 1)
        return reference(kind, identity) if kind in {"workflow", "automation"} else key

    for field in ("order", "hidden"):
        config[field] = [action(key) for key in config[field]]
    if config["hero"]:
        config["hero"] = action(config["hero"])
    for field in ("labels", "descriptions", "forms", "entries", "prompts", "presets", "asks", "choices"):
        config[field] = {action(key): item for key, item in config[field].items()}
    if config.get("icons"):
        config["icons"] = {action(key): icon for key, icon in config["icons"].items()}
    # Translations name the same entries as the page they translate.
    for overlay in (config.get("i18n") or {}).values():
        for field in ("labels", "descriptions", "forms", "entries", "prompts", "presets", "choices"):
            if isinstance(overlay.get(field), dict):
                overlay[field] = {action(key): item for key, item in overlay[field].items()}
    targets = {}
    for key, target in config["targets"].items():
        kind, identity = app_target_kind_and_id(key, target)
        targets[action(key)] = reference("agent" if kind == "chat" else kind, identity)
    config["targets"] = targets
    for key, source in config.get("data_sources", {}).items():
        source["producer_workflows"] = [
            reference("workflow_definition", ref.removeprefix("workflow_definition:"))
            for ref in source.get("producer_workflows", [])
        ]
        for launch in source.get("launch_workflows", []):
            launch["workflow"] = reference("workflow", launch["workflow"].removeprefix("workflow:"))
        if source["type"] == "workspace_stats":
            missing = set(source["stat_keys"]) - (stat_keys or set())
            if missing:
                raise ValueError(f"App source {key!r} Stats are not included in this Blueprint: {', '.join(sorted(missing))}")
    if config["sections"] is not None and stat_keys is not None:
        named = {ref.removeprefix("stat:") for section in config["sections"]
                 for ref in section.get("items") or [] if ref.startswith("stat:")}
        missing = named - stat_keys
        if missing:
            raise ValueError(f"App metrics name Stats that are not included in this Blueprint: {', '.join(sorted(missing))}")
    if config["sections"] is not None:
        config["sections"] = [
            {**section, "source": reference("workflow", section["source"].removeprefix("workflow:"))}
            if (section["source"] or "").startswith("workflow:") else section
            for section in config["sections"]
        ]
    # Local file URLs cannot refer to the same asset in another deployment;
    # the platform's own pictures and loops are the same file everywhere, and
    # a package carries its own.
    for field in ("cover_image_url", "motion_cover_url", "motion_cover_source_url"):
        url = config.get(field)
        if url and not is_portable_asset_url(url):
            raise ValueError(f"App {field} needs a portable HTTPS asset URL before export")
    return config


def validate_portable_app_view(payload: dict[str, Any]) -> None:
    recipe = payload.get("recipe") or {}
    value = (recipe.get("operating_model") or {}).get("app_view")
    if value is None:
        return
    workflows = {w["slug"]: w["slug"] for w in recipe.get("workflows") or [] if not w.get("internal")}
    agents = {a["agent_slug"]: a["agent_slug"] for a in recipe.get("subscriptions") or [] if a.get("agent_slug")}
    jobs = {j["job_id"]: j["job_id"] for j in recipe.get("scheduled_jobs") or []}
    definitions = {w["slug"]: w["slug"] for w in recipe.get("workflows") or []}
    stat_keys = {s["key"] for s in recipe.get("stats") or [] if s.get("key")}
    config = remap_app_view(value, workflows=workflows, agents=agents, automations=jobs,
                           workflow_definitions=definitions, stat_keys=stat_keys)
    from packages.core.services.workspace_app_view import validate_app_view_data_contract
    validate_app_view_data_contract(config.get("data_sources", {}), config.get("components", []), catalog={
        "workflow_definitions": definitions, "workflow_bindings": workflows, "stat_keys": stat_keys,
        "workflows": recipe.get("workflows") or [],
    })


async def remap_workspace_app_view(db: Any, workspace: Any, value: Any, *, to_runtime: bool) -> dict[str, Any]:
    """Project configuration upgrades using the same identities as export."""
    from sqlalchemy import select
    from packages.core.blueprints.exporter import _workspace_workflow_export_rows, _portable_agent_slug
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.models.workspace_stat import WorkspaceStat

    workflow_rows = await _workspace_workflow_export_rows(db, workspace.entity_id, workspace.id)
    agents = (
        (
            await db.execute(
                select(Agent)
                .join(
                    AgentSubscription,
                    AgentSubscription.agent_id == Agent.id,
                )
                .where(
                    AgentSubscription.workspace_id == workspace.id,
                    AgentSubscription.entity_id == workspace.entity_id,
                    AgentSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    jobs = (
        (
            await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.entity_id == workspace.entity_id,
                    ScheduledJob.agent_initiative_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    maps = {
        "workflows": {binding.id: slug for _, binding, slug, _ in workflow_rows if binding is not None},
        "workflow_definitions": {workflow.id: slug for workflow, _, slug, _ in workflow_rows},
        "agents": {a.id: _portable_agent_slug(a) for a in agents},
        "automations": {j.job_id: j.job_id.removesuffix(f"-{workspace.id[-8:]}") for j in jobs},
    }
    if to_runtime:
        for name, mapping in maps.items():
            if len(set(mapping.values())) != len(mapping):
                raise ValueError(f"App {name} identities are ambiguous in this Workspace")
        maps = {name: {v: k for k, v in mapping.items()} for name, mapping in maps.items()}
    stat_keys = set((await db.execute(select(WorkspaceStat.key).where(
        WorkspaceStat.workspace_id == workspace.id, WorkspaceStat.entity_id == workspace.entity_id,
        WorkspaceStat.status == "active",
    ))).scalars())
    return remap_app_view(value, **maps, stat_keys=stat_keys)
