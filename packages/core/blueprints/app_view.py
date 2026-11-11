"""Translate App presentation references at the Blueprint boundary.

App configuration contains no capabilities of its own. Every runner must resolve
against the Workspace components exported or installed in the same transaction.
"""

from __future__ import annotations

import re
from typing import Any

from packages.core.services.workspace_app_view import (
    app_target_kind_and_id,
    AppViewContractError,
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


def without_local_pictures(value: dict[str, Any]) -> dict[str, Any]:
    """The page without what only this deployment can open: an uploaded cover
    and the motion cover made for it."""

    def portable(field: str) -> bool:
        return not value.get(field) or is_portable_asset_url(value[field])

    page = dict(value)
    if not portable("cover_image_url"):
        page.pop("cover_image_url")
    if not (portable("motion_cover_url") and portable("motion_cover_source_url")):
        page.update(motion_cover_url=None, motion_cover_source_url=None)
    return page


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
    problems: list[str] = []

    def reference(kind: str, identity: str) -> str:
        if kind == "agent" and identity == "default":
            return "agent:default"
        resolved = maps[kind].get(identity)
        if not resolved:
            problem = (f"App {kind} target {identity!r} is not included in this Blueprint: "
                       f"it includes {', '.join(sorted(maps[kind])) or 'none'}")
            if problem not in problems:
                problems.append(problem)
            return f"{kind}:{identity}"
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
    # An item's own action (remake this episode, script this slot) and a
    # list's Add button name an App entry like the tiles do.
    for component in config.get("components", []):
        for block in component.get("blocks", []):
            for field in ("item_action", "result_action", "plan_action", "add_action"):
                if isinstance(block.get(field), dict):
                    block[field]["action"] = action(block[field]["action"])
            for item_action in block.get("item_actions") or []:
                if isinstance(item_action, dict):
                    item_action["action"] = action(item_action["action"])
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
                problems.append(f"App source {key!r} Stats are not included in this Blueprint: {', '.join(sorted(missing))}")
    if config["sections"] is not None and stat_keys is not None:
        named = {ref.removeprefix("stat:") for section in config["sections"]
                 for ref in section.get("items") or [] if ref.startswith("stat:")}
        missing = named - stat_keys
        if missing:
            problems.append(f"App metrics name Stats that are not included in this Blueprint: {', '.join(sorted(missing))}")
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
            problems.append(f"App {field} needs a portable HTTPS asset URL before export")
    if problems:
        raise AppViewContractError(problems)
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


async def remap_workspace_app_view(
    db: Any, workspace: Any, value: Any, *, to_runtime: bool, keep_local_pictures: bool = False,
) -> dict[str, Any]:
    """Project configuration upgrades using the same identities as export.

    ``keep_local_pictures`` is for reading and restoring this Workspace's own
    page, where an upload is a real part of it; only leaving the deployment
    needs a picture to be portable.
    """
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
    if not keep_local_pictures:
        return remap_app_view(value, **maps, stat_keys=stat_keys)
    config = remap_app_view(without_local_pictures(value), **maps, stat_keys=stat_keys)
    pictures = ("cover_image_url", "motion_cover_url", "motion_cover_source_url")
    return {**config, **{name: value[name] for name in pictures if value.get(name)}}
