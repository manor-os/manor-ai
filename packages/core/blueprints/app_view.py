"""Translate App presentation references at the Blueprint boundary.

App configuration contains no capabilities of its own. Every runner must resolve
against the Workspace components exported or installed in the same transaction.
"""

from __future__ import annotations

from typing import Any

from packages.core.services.workspace_app_view import (
    app_target_kind_and_id,
    normalize_app_view_config,
)


def remap_app_view(
    value: Any,
    *,
    workflows: dict[str, str],
    agents: dict[str, str],
    automations: dict[str, str],
) -> dict[str, Any]:
    config = normalize_app_view_config(value)
    maps = {"workflow": workflows, "agent": agents, "automation": automations}

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
    for field in ("labels", "descriptions", "forms", "entries", "prompts"):
        config[field] = {action(key): item for key, item in config[field].items()}
    targets = {}
    for key, target in config["targets"].items():
        kind, identity = app_target_kind_and_id(key, target)
        targets[action(key)] = reference("agent" if kind == "chat" else kind, identity)
    config["targets"] = targets
    # Local file URLs cannot refer to the same asset in another deployment.
    for field in ("motion_cover_url", "motion_cover_source_url"):
        if config[field] and not config[field].startswith("https://"):
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
    remap_app_view(value, workflows=workflows, agents=agents, automations=jobs)


async def remap_workspace_app_view(db: Any, workspace: Any, value: Any, *, to_runtime: bool) -> dict[str, Any]:
    """Project configuration upgrades using the same identities as export."""
    from sqlalchemy import select
    from packages.core.blueprints.exporter import _workspace_workflow_export_rows, _portable_agent_slug
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription

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
        "agents": {a.id: _portable_agent_slug(a) for a in agents},
        "automations": {j.job_id: j.job_id.removesuffix(f"-{workspace.id[-8:]}") for j in jobs},
    }
    if to_runtime:
        for name, mapping in maps.items():
            if len(set(mapping.values())) != len(mapping):
                raise ValueError(f"App {name} identities are ambiguous in this Workspace")
        maps = {name: {v: k for k, v in mapping.items()} for name, mapping in maps.items()}
    return remap_app_view(value, **maps)
