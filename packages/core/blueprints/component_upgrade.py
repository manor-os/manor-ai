"""Reviewed, reversible reconciliation of Blueprint-owned reusable components.

The caller owns the Workspace lifecycle fence and transaction. Public plans
contain portable configuration only; restore receipts contain changed local
rows, never task/run/chat history or integration credential values.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from sqlalchemy import or_, select

from packages.core.blueprints.exporter import (
    _installed_marketplace_links_by_local_id,
    _portable_skill_identity,
    _without_nonportable_references,
)
from packages.core.blueprints.freshness import installed_blueprint_record
from packages.core.blueprints.installer import (
    InstallError,
    _blueprint_workflow_definition_values,
    _blueprint_workflow_trigger_config,
    _enforce_governance_against_agent,
    _install_embedded_agent,
    _install_embedded_skill,
    _install_workflow,
    _install_workflow_binding,
    blueprint_workflow_installation_source_id,
)
from packages.core.blueprints.upgrade import (
    BlueprintUpgradeAccessDeniedError,
    BlueprintUpgradeIncompleteError,
    BlueprintUpgradePlanChangedError,
    _require_component_access,
    _require_component_edit_access,
    _workflow_source_template_ids,
)
from packages.core.blueprints.workflow_dependencies import WorkflowDependencyFactory
from packages.core.governance.policy import WorkspacePolicy
from packages.core.models.base import generate_ulid
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import (
    WorkflowBinding, WorkflowDefinition, WorkflowTemplateInstallation,
)
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition
from packages.core.revisions import bump_revision
from packages.core.services.agent_runtime_config import normalize_agent_runtime_config
from packages.core.services.marketplace_resource_links import rekey_marketplace_resource_link
from packages.core.services.reusable_resource_locks import (
    lock_reusable_resource_lifecycle,
    lock_reusable_resource_payload_references,
    reusable_resource_ids_from_payload,
)
from packages.core.services.sensitive_data import sanitize_sensitive_payload


_MODELS = {"agent": Agent, "skill": Skill, "workflow": WorkflowDefinition}
_AGENT_FIELDS = ("name", "slug", "description", "system_prompt", "config", "category", "tags", "version", "status")
_SKILL_FIELDS = ("name", "slug", "display_name", "description", "system_prompt", "tools", "input_schema", "output_format", "category", "tags", "version", "config", "status")
_FLOW_FIELDS = ("name", "description", "trigger_type", "trigger_config", "steps", "variables", "category", "tags", "is_active", "version", "status")
_MEMORY_FIELDS = ("memory_type", "scope", "content", "importance", "confidence")
_BINDING_FIELDS = ("name", "trigger_type", "trigger_config", "variables", "config", "enabled", "status")
_LOCAL_CONFIG_KEYS = {"source_blueprint_id", "source_blueprint_component_key", "blueprint_component_retired"}
_RECEIPT_CONFIG_KEYS = _LOCAL_CONFIG_KEYS | {"source", "source_template_id", "workspace_blueprint_workflow_slug"}
_JOURNAL_MODELS = {
    cls.__name__: cls for cls in (
        Agent, Skill, WorkflowDefinition, AgentToolBinding, AgentSkillBinding,
        AgentMCPBinding, AgentMemory, WorkflowBinding, WorkflowTemplateInstallation,
        MarketplaceResourceLink, AgentSubscription,
    )
}
_JOURNAL_FIELDS = {
    "Agent": (*_AGENT_FIELDS, "entity_id", "workspace_id", "owner_user_id", "visibility", "is_public", "is_template", "source"),
    "Skill": (*_SKILL_FIELDS, "entity_id", "workspace_id", "owner_user_id", "visibility", "is_public"),
    "WorkflowDefinition": (*_FLOW_FIELDS, "entity_id", "workspace_id", "created_by", "visibility"),
    "AgentToolBinding": ("agent_id", "tool_id"),
    "AgentSkillBinding": ("agent_id", "skill_id", "status"),
    "AgentMCPBinding": ("agent_id", "mcp_server_id", "allowed_tools", "status"),
    "AgentMemory": (*_MEMORY_FIELDS, "agent_id", "entity_id", "workspace_id", "user_id", "source", "metadata_", "status"),
    "WorkflowBinding": (*_BINDING_FIELDS, "entity_id", "workspace_id", "workflow_id"),
    "WorkflowTemplateInstallation": ("entity_id", "template_id", "component_key", "workflow_id", "installed_version", "installed_by", "source_type", "installation_metadata"),
    "MarketplaceResourceLink": ("entity_id", "marketplace_source", "marketplace_resource_type", "marketplace_resource_id", "relationship", "scope_type", "scope_id", "local_resource_type", "local_resource_id", "component_key", "marketplace_version", "linked_by", "link_metadata"),
    "AgentSubscription": ("entity_id", "workspace_id", "agent_id", "status"),
}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _portable(value: Any) -> Any:
    projected = _without_nonportable_references(deepcopy(value))
    if isinstance(projected, dict):
        projected = {key: val for key, val in projected.items() if key not in _LOCAL_CONFIG_KEYS}
    return sanitize_sensitive_payload(projected)


def _replace_portable(existing: Any, desired: Any) -> Any:
    """Replace portable config while leaving local/credential configuration alone."""
    if not isinstance(existing, dict) or not isinstance(desired, dict):
        return deepcopy(desired)
    safe = _portable(existing)
    retained = {key: deepcopy(value) for key, value in existing.items() if key not in safe}
    for key, value in existing.items():
        if key not in desired and key in safe and isinstance(value, dict):
            local_children = _replace_portable(value, {})
            if local_children:
                retained[key] = local_children
    for key, value in desired.items():
        old = existing.get(key)
        retained[key] = _replace_portable(old, value) if isinstance(old, dict) and isinstance(value, dict) else deepcopy(value)
    # Sensitive-data redaction may keep a key but change its value. Such data is
    # local setup and must neither be removed nor restored from a redacted copy.
    for key, value in existing.items():
        if key in safe and safe[key] != _without_nonportable_references(value):
            retained[key] = deepcopy(value)
    return retained


def _specs(payload: dict) -> dict[str, dict]:
    embedded = payload.get("embedded") or {}
    result = {}
    for kind, values in (("skill", embedded.get("skills") or []), ("agent", embedded.get("agents") or []), ("workflow", (payload.get("recipe") or {}).get("workflows") or [])):
        for spec in values:
            key = str(spec.get("component_id") or spec.get("id") or spec.get("slug") or "").strip()
            if not key or not spec.get("slug"):
                raise InstallError(f"Blueprint {kind} requires a stable component key and slug")
            identity = f"{kind}:{key}"
            if identity in result:
                raise InstallError(f"Duplicate Blueprint component {identity}")
            result[identity] = deepcopy(spec)
            if kind == "agent":
                refs = list(spec.get("skill_binding_refs") or [])
                exact_slugs = {ref.get("slug") for ref in refs}
                for requirement in ((payload.get("contract") or {}).get("requires") or {}).get("skills") or []:
                    if requirement.get("slug") in (spec.get("skill_bindings") or []) and requirement.get("marketplace_id") and requirement["slug"] not in exact_slugs:
                        refs.append({field: requirement[field] for field in ("slug", "marketplace_id", "marketplace_source") if field in requirement})
                if refs:
                    result[identity]["skill_binding_refs"] = refs
    workflow_slugs = {key.split(":", 1)[1]: spec["slug"] for key, spec in result.items() if key.startswith("workflow:")}
    workflow_slugs.update({slug: slug for slug in workflow_slugs.values()})
    for key, spec in result.items():
        if not key.startswith("workflow:"):
            continue
        for step in spec.get("steps") or []:
            if (step.get("type") or step.get("kind")) not in WorkflowDependencyFactory.STEP_TYPES:
                continue
            config = dict(step.get("config") or {})
            target = config.get("source_workflow_key") or config.get("workflow_id")
            if target in workflow_slugs:
                config.pop("workflow_id", None)
                config["source_workflow_key"] = workflow_slugs[target]
                step["config"] = config
    return result


def _source(workspace: Any, payload: dict) -> tuple[str, list[str]]:
    record = installed_blueprint_record(workspace.settings)
    source_ids = _workflow_source_template_ids(record, payload, record.get("blueprint_id"))
    if not source_ids:
        raise BlueprintUpgradeIncompleteError("Workspace has no exact Blueprint source identity")
    return source_ids[0], source_ids


async def _rows(db, model, *conditions, lock: bool = False):
    stmt = select(model).where(*conditions).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


async def _inventory(db, *, workspace, payload, baseline=None, lock=False):
    """Exact provenance only: a same-name local resource is never adopted."""
    source_id, source_ids = _source(workspace, payload)
    specs = _specs(payload)
    result: dict[str, Any] = {}
    links = await _rows(db, MarketplaceResourceLink,
        MarketplaceResourceLink.entity_id == workspace.entity_id,
        MarketplaceResourceLink.marketplace_resource_type == "workspace_blueprint",
        MarketplaceResourceLink.marketplace_resource_id.in_(source_ids),
        MarketplaceResourceLink.relationship == "installed_component",
        MarketplaceResourceLink.scope_type == "workspace",
        MarketplaceResourceLink.scope_id == workspace.id, lock=lock)
    for link in links:
        kind = link.local_resource_type
        if kind not in _MODELS:
            continue
        row = await db.get(_MODELS[kind], link.local_resource_id, populate_existing=True, with_for_update=lock)
        if row is not None and row.entity_id == workspace.entity_id and not getattr(row, "deleted_at", None):
            result[f"{kind}:{link.component_key}"] = row
    # Historical installers wrote these exact source receipts before the
    # Marketplace link table existed. No slug-only fallback is permitted.
    for kind in ("agent", "skill"):
        model = _MODELS[kind]
        legacy = {}
        for row in await _rows(db, model, model.entity_id == workspace.entity_id, model.config["source_blueprint_id"].as_string().in_(source_ids), lock=lock):
            config = row.config or {}
            if config.get("source_blueprint_id") not in source_ids or getattr(row, "deleted_at", None):
                continue
            component_key = config.get("source_blueprint_component_key") or row.slug
            key = f"{kind}:{component_key}"
            if not component_key or key in result:
                continue
            if row.workspace_id != workspace.id:
                if kind == "agent":
                    deployments = await _rows(db, AgentSubscription, AgentSubscription.workspace_id == workspace.id, AgentSubscription.agent_id == row.id)
                else:
                    deployments = (await db.execute(select(AgentSkillBinding).join(AgentSubscription, AgentSubscription.agent_id == AgentSkillBinding.agent_id).where(AgentSubscription.workspace_id == workspace.id, AgentSkillBinding.skill_id == row.id))).scalars().all()
                    if not deployments:
                        from packages.core.models.scheduler import ScheduledJob
                        deployments = await _rows(db, ScheduledJob, ScheduledJob.workspace_id == workspace.id, ScheduledJob.execution_target["skill_id"].as_string() == row.id)
                if not deployments:
                    continue
            if key in legacy and legacy[key].id != row.id:
                raise BlueprintUpgradeIncompleteError(f"Blueprint {kind} {component_key!r} has ambiguous legacy source mappings")
            legacy[key] = row
        result.update(legacy)
    template_ids = source_ids + [blueprint_workflow_installation_source_id(sid, workspace.id) for sid in source_ids]
    for mapping in await _rows(db, WorkflowTemplateInstallation, WorkflowTemplateInstallation.entity_id == workspace.entity_id, WorkflowTemplateInstallation.template_id.in_(template_ids), lock=lock):
        row = await db.get(WorkflowDefinition, mapping.workflow_id, populate_existing=True, with_for_update=lock)
        if row is None or row.entity_id != workspace.entity_id:
            continue
        if row.id in {existing.id for existing in result.values()}:
            continue
        matches = [key for key, spec in specs.items() if key.startswith("workflow:") and spec["slug"] == mapping.component_key]
        key = matches[0] if len(matches) == 1 else f"workflow:{mapping.component_key}"
        # A legacy entity definition is accepted only through its exact mapping
        # and its Workspace binding, or a stored Workspace baseline.
        if row.workspace_id != workspace.id:
            bound = await _rows(db, WorkflowBinding, WorkflowBinding.workflow_id == row.id, WorkflowBinding.workspace_id == workspace.id)
            if not bound and key not in (baseline or {}):
                continue
        result.setdefault(key, row)
    return result


def _ordered(values: list[dict]) -> list[dict]:
    return sorted(values, key=lambda value: json.dumps(value, sort_keys=True, default=str))


def _desired(kind: str, spec: dict, *, current=None) -> dict:
    slug = spec["slug"]
    if kind == "skill":
        result = {
            "name": spec.get("name") or slug, "slug": slug,
            "display_name": spec.get("display_name") or spec.get("name") or slug,
            "description": spec.get("description"), "system_prompt": spec.get("system_prompt") or "",
            "tools": sorted(set(spec.get("tools") or [])), "input_schema": dict(spec.get("input_schema") or {}),
            "output_format": spec.get("output_format") or "text", "category": spec.get("category"),
            "tags": list(spec.get("tags") or []), "version": spec.get("version") or "1.0.0",
            "config": _portable(dict(spec.get("config") or {})), "status": "active",
        }
    elif kind == "agent":
        config = normalize_agent_runtime_config(spec.get("config"))
        capabilities = sorted(set(spec.get("business_capabilities") or config.get("business_capabilities") or []))
        if capabilities:
            config["business_capabilities"] = capabilities
        result = {
            "name": spec.get("name") or slug, "slug": slug, "description": spec.get("description"),
            "system_prompt": spec.get("system_prompt"), "config": _portable(config),
            "category": spec.get("category"), "tags": list(spec.get("tags") or []),
            "version": spec.get("version") or "1.0", "status": "active",
            "tool_bindings": sorted(set(spec.get("tool_bindings") or [])),
            "skill_bindings": sorted(set(spec.get("skill_bindings") or []) | {str(ref.get("slug")) for ref in spec.get("skill_binding_refs") or []}),
            "skill_binding_refs": _ordered([
                {**ref, "marketplace_source": ref.get("marketplace_source") or "platform"}
                for ref in spec.get("skill_binding_refs") or []
            ]),
            "mcp_bindings": _ordered([{"server_slug": b["server_slug"], "allowed_tools": b.get("allowed_tools")} for b in spec.get("mcp_bindings") or []]),
            "starter_memory": _ordered([{
                "memory_type": m.get("memory_type") or "instruction", "scope": m.get("scope"),
                "content": m.get("content") or "", "importance": int(m.get("importance") or 5),
                "confidence": float(m.get("confidence") or 1.0),
            } for m in spec.get("starter_memory") or []]),
        }
    else:
        result = _blueprint_workflow_definition_values(spec)
        # Version labels cannot downgrade an installed Flow. Graph changes bump
        # the runtime version independently of the author's portable release.
        result["version"] = max(result["version"], int((current or {}).get("version") or 1))
        result["binding"] = None if spec.get("internal") or spec.get("trigger_type") == "schedule" else {
            "name": spec.get("name") or slug,
            "trigger_type": spec.get("trigger_type") or "manual",
            "trigger_config": _blueprint_workflow_trigger_config(spec),
            "variables": result["variables"],
            "config": {
                **dict(spec.get("binding_config") or {}),
                **({"proposal_authorization": spec["proposal_authorization"]} if isinstance(spec.get("proposal_authorization"), dict) else {}),
            },
            "enabled": bool(spec.get("enabled", True)), "status": spec.get("status") or "active",
        }
    if kind == "agent" and current:
        exact_slugs = {ref["slug"] for ref in result["skill_binding_refs"]}
        result["skill_binding_refs"] = _ordered(result["skill_binding_refs"] + [
            ref for ref in current.get("skill_binding_refs") or []
            if ref.get("slug") in result["skill_bindings"] and ref.get("slug") not in exact_slugs
        ])
    return _portable(result)


async def _content(db, *, workspace, key, row, inventory, specs=None):
    kind = key.split(":", 1)[0]
    fields = {"agent": _AGENT_FIELDS, "skill": _SKILL_FIELDS, "workflow": _FLOW_FIELDS}[kind]
    content = {field: deepcopy(getattr(row, field)) for field in fields}
    if kind == "skill":
        content["tools"] = sorted(set(content["tools"] or []))
    elif kind == "agent":
        tools = (await db.execute(select(ToolDefinition.name).join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id).where(AgentToolBinding.agent_id == row.id))).scalars().all()
        content["tool_bindings"] = sorted(set(tools))
        skills = (await db.execute(select(Skill).join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id).where(AgentSkillBinding.agent_id == row.id, AgentSkillBinding.status == "active"))).scalars().all()
        content["skill_bindings"] = sorted({str(skill.slug) for skill in skills})
        skill_links = await _installed_marketplace_links_by_local_id(db, entity_id=workspace.entity_id, local_resource_type="skill", local_resource_ids=[skill.id for skill in skills])
        skill_refs = []
        for skill in skills:
            identity = await _portable_skill_identity(db, skill, entity_id=workspace.entity_id, source_link=skill_links.get(skill.id), include_skills=True)
            if identity.marketplace_id:
                skill_refs.append({"slug": identity.slug, "marketplace_id": identity.marketplace_id, "marketplace_source": identity.marketplace_source})
        content["skill_binding_refs"] = _ordered(skill_refs)
        mcp = (await db.execute(select(AgentMCPBinding, MCPServer.server_key).join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id).where(AgentMCPBinding.agent_id == row.id, AgentMCPBinding.status == "active"))).all()
        content["mcp_bindings"] = _ordered([{"server_slug": server_key, "allowed_tools": binding.allowed_tools} for binding, server_key in mcp])
        memories = await _rows(db, AgentMemory, AgentMemory.agent_id == row.id, AgentMemory.workspace_id == workspace.id, AgentMemory.source == "blueprint", AgentMemory.user_id.is_(None), AgentMemory.status == "active")
        content["starter_memory"] = _ordered([{field: getattr(memory, field) for field in _MEMORY_FIELDS} for memory in memories])
    else:
        flow_keys = {item.id: (specs or {}).get(component_key, {}).get("slug") or component_key.split(":", 1)[1] for component_key, item in inventory.items() if component_key.startswith("workflow:")}
        try:
            content["steps"] = WorkflowDependencyFactory.to_portable(content["steps"], workflow_key_by_id=flow_keys)
        except ValueError:
            # A user-added reference remains visible as an opaque local target;
            # it cannot silently become a portable Blueprint dependency.
            content["steps"] = deepcopy(content["steps"])
        bindings = await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id)
        declared = (specs or {}).get(key) or {}
        expects_binding = bool(declared) and not declared.get("internal") and declared.get("trigger_type") != "schedule"
        bindings = [binding for binding in bindings if (binding.config or {}).get("source") == "blueprint" and (binding.status == "active" or expects_binding)]
        if len(bindings) > 1:
            exact = [binding for binding in bindings if (binding.config or {}).get("workspace_blueprint_workflow_slug") == declared.get("slug")]
            active = [binding for binding in exact if binding.status == "active"]
            bindings = active or exact or bindings
        if len(bindings) > 1:
            raise BlueprintUpgradeIncompleteError(f"Blueprint Flow {row.name!r} has ambiguous active bindings")
        binding = bindings[0] if bindings else None
        content["binding"] = {field: deepcopy(getattr(binding, field)) for field in _BINDING_FIELDS} if binding else None
        if binding:
            content["binding"]["config"] = {k: v for k, v in (binding.config or {}).items() if k not in {"source", "source_template_id", "workspace_blueprint_workflow_slug"}}
    return _portable(content)


async def capture_components(db, *, workspace, payload: dict, baseline: dict | None = None, actor=None) -> dict:
    inventory = await _inventory(db, workspace=workspace, payload=payload, baseline=baseline)
    specs = _specs(payload)
    result = {}
    for key, row in sorted(inventory.items()):
        if actor is not None:
            from packages.core.models.permission import Capability
            await _require_component_access(db, row=row, kind=key.split(":", 1)[0], entity_id=workspace.entity_id,
                                            actor=actor, capability=Capability.VIEW)
        content = await _content(db, workspace=workspace, key=key, row=row, inventory=inventory, specs=specs)
        result[key] = {"id": row.id, "kind": key.split(":", 1)[0], "slug": getattr(row, "slug", None) or key.split(":", 1)[1], "content": content,
                       "state_fingerprint": await _component_state_fingerprint(db, workspace=workspace, key=key, row=row),
                       "source": _desired(key.split(":", 1)[0], specs[key], current=content) if key in specs else None}
    return result


async def plan_components(db, *, workspace, payload: dict, baseline: dict | None = None, actor=None) -> dict:
    current = await capture_components(db, workspace=workspace, payload=payload, baseline=baseline, actor=actor)
    specs = _specs(payload)
    items = []
    for key in sorted(set(specs) | set(baseline or {})):
        before = (current.get(key) or {}).get("content")
        prior = (baseline or {}).get(key)
        desired = _desired(key.split(":", 1)[0], specs[key], current=before) if key in specs else None
        if prior and prior.get("source") is None and desired is None:
            continue  # Already retired by an earlier reviewed upgrade.
        if before == desired:
            action = "unchanged"
        elif prior is not None and "source" in prior and desired == prior["source"]:
            # The author did not change this component. Both an earlier
            # reviewed customization and later local edits remain local.
            action = "unchanged"
        elif before is None and desired is not None:
            action = "missing"
        elif prior is not None and before == prior.get("content") and prior.get("source", prior.get("content")) == prior.get("content"):
            action = "update"
        else:
            action = "keep_yours"
        items.append({
            "key": key, "kind": key.split(":", 1)[0], "slug": specs.get(key, {}).get("slug") or (current.get(key) or {}).get("slug") or key.split(":", 1)[1],
            "name": (desired or before or {}).get("name") or key, "action": action,
            "current_content": before, "new_content": desired,
            "baseline_known": prior is not None,
            "baseline_content": prior.get("source", prior.get("content")) if prior else None,
            "expected_fingerprint": _hash(current.get(key)),
            "changes": ["Remove Blueprint deployment" if desired is None else "Install Blueprint component" if before is None else "Component configuration and bindings"],
        })
    return {"items": items, "current": current, "source": specs}


def _snapshot(row) -> dict:
    model = type(row).__name__
    values = {field: deepcopy(getattr(row, field)) for field in _JOURNAL_FIELDS[model]}
    for field in ("config", "trigger_config", "metadata_"):
        if field in values:
            # Keep provenance keys (non-secret local identities) for undo, but
            # never include credential/runtime config values in receipts.
            raw = values[field] or {}
            values[field] = {**_portable(raw), **{key: raw[key] for key in _RECEIPT_CONFIG_KEYS if key in raw}}
    primary = {column.key: getattr(row, column.key) for column in row.__table__.primary_key.columns}
    # Overrides stay in their existing credential/setup rows. A digest still
    # detects a later edit before undo could remove a newly created binding.
    guard = {field: getattr(row, field) for field in ("config", "config_override") if hasattr(row, field)}
    return {"model": model, "identity": primary, "values": values, "revision": getattr(row, "revision", None), "local_config_fingerprint": _hash(guard)}


async def _component_state_fingerprint(db, *, workspace, key, row):
    """Hash runtime identities and local overrides without exposing their values."""
    rows = [row]
    kind = key.split(":", 1)[0]
    if kind == "agent":
        for model in (AgentToolBinding, AgentSkillBinding, AgentMCPBinding):
            rows.extend(await _rows(db, model, model.agent_id == row.id))
        rows.extend(await _rows(db, AgentMemory, AgentMemory.agent_id == row.id, AgentMemory.workspace_id == workspace.id, AgentMemory.source == "blueprint"))
    elif kind == "workflow":
        rows.extend(await _rows(db, WorkflowBinding, WorkflowBinding.workflow_id == row.id, WorkflowBinding.workspace_id == workspace.id))
    values = []
    for candidate in rows:
        values.append({"model": type(candidate).__name__, **{
            column.key: getattr(candidate, column.key if column.key != "metadata" else "metadata_")
            for column in candidate.__table__.columns
            if column.key not in {"created_at", "updated_at"}
        }})
    return _hash(_ordered(values))


async def _journal_state(db, *, workspace, inventory):
    result = {}
    rows = list(inventory.values())
    agent_ids = [row.id for key, row in inventory.items() if key.startswith("agent:")]
    flow_ids = [row.id for key, row in inventory.items() if key.startswith("workflow:")]
    if agent_ids:
        for model in (AgentToolBinding, AgentSkillBinding, AgentMCPBinding):
            rows.extend(await _rows(db, model, model.agent_id.in_(agent_ids), lock=True))
        rows.extend(await _rows(db, AgentMemory, AgentMemory.agent_id.in_(agent_ids), AgentMemory.workspace_id == workspace.id, AgentMemory.source == "blueprint", lock=True))
        rows.extend(await _rows(db, AgentSubscription, AgentSubscription.workspace_id == workspace.id, AgentSubscription.agent_id.in_(agent_ids), lock=True))
    if flow_ids:
        rows.extend(await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id.in_(flow_ids), lock=True))
        rows.extend(await _rows(db, WorkflowTemplateInstallation, WorkflowTemplateInstallation.workflow_id.in_(flow_ids), lock=True))
    rows.extend(await _rows(db, MarketplaceResourceLink, MarketplaceResourceLink.entity_id == workspace.entity_id, MarketplaceResourceLink.scope_type == "workspace", MarketplaceResourceLink.scope_id == workspace.id, MarketplaceResourceLink.relationship == "installed_component", lock=True))
    for row in rows:
        snap = _snapshot(row)
        result[f"{snap['model']}:{_hash(snap['identity'])}"] = snap
    return result


async def _link(db, *, workspace, source_id, key, row, by_user_id):
    # Explicit isolation may replace a target local id. Normal Marketplace
    # recording deliberately rejects that operation, so retire the exact old
    # target mapping before recording the new identity in the same transaction.
    source_ids = _workflow_source_template_ids(installed_blueprint_record(workspace.settings), None, source_id)
    for link in await _rows(db, MarketplaceResourceLink, MarketplaceResourceLink.entity_id == workspace.entity_id,
        MarketplaceResourceLink.marketplace_resource_type == "workspace_blueprint", MarketplaceResourceLink.marketplace_resource_id.in_(source_ids),
        MarketplaceResourceLink.scope_type == "workspace", MarketplaceResourceLink.scope_id == workspace.id, MarketplaceResourceLink.relationship == "installed_component", MarketplaceResourceLink.local_resource_type == key.split(":", 1)[0], MarketplaceResourceLink.component_key == key.split(":", 1)[1], lock=True):
        if link.local_resource_id != row.id:
            await db.delete(link)
    await db.flush()
    await rekey_marketplace_resource_link(db, entity_id=workspace.entity_id, marketplace_resource_type="workspace_blueprint", marketplace_resource_id=source_id, relationship="installed_component", scope_type="workspace", scope_id=workspace.id, local_resource_type=key.split(":", 1)[0], local_resource_id=row.id, component_key=key.split(":", 1)[1], marketplace_version=str(row.version), linked_by=by_user_id, metadata={"source_slug": getattr(row, "slug", None) or key.split(":", 1)[1]})


async def _is_shared(db, *, workspace, kind, row) -> bool:
    if row.workspace_id != workspace.id or getattr(row, "is_public", False) or row.visibility != "workspace":
        return True
    links = await _rows(db, MarketplaceResourceLink, MarketplaceResourceLink.entity_id == workspace.entity_id, MarketplaceResourceLink.local_resource_type == kind, MarketplaceResourceLink.local_resource_id == row.id, or_(MarketplaceResourceLink.scope_type != "workspace", MarketplaceResourceLink.scope_id != workspace.id))
    if links:
        return True
    # Direct schedule targets and graphs can reuse resources without a
    # deployment row. They share the reusable-resource lifecycle lock.
    from packages.core.models.scheduler import ScheduledJob
    foreign = await _rows(db, ScheduledJob, ScheduledJob.entity_id == workspace.entity_id, or_(ScheduledJob.workspace_id.is_(None), ScheduledJob.workspace_id != workspace.id))
    index = {"agent": 0, "skill": 1, "workflow": 2}[kind]
    for job in foreign:
        if (kind == "agent" and job.agent_id == row.id) or row.id in reusable_resource_ids_from_payload(job.execution_target)[index]:
            return True
    definitions = await _rows(db, WorkflowDefinition, WorkflowDefinition.entity_id == workspace.entity_id)
    external_bindings = await _rows(db, WorkflowBinding, WorkflowBinding.entity_id == workspace.entity_id, or_(WorkflowBinding.workspace_id.is_(None), WorkflowBinding.workspace_id != workspace.id))
    external_ids = {binding.workflow_id for binding in external_bindings} | {definition.id for definition in definitions if definition.workspace_id != workspace.id}
    references = {definition.id: reusable_resource_ids_from_payload(definition.steps) for definition in definitions}
    pending = list(external_ids)
    visited = set()
    while pending:
        workflow_id = pending.pop()
        if workflow_id in visited:
            continue
        visited.add(workflow_id)
        refs = references.get(workflow_id, (set(), set(), set()))
        if row.id in refs[index]:
            return True
        pending.extend(refs[2] - visited)
    if kind == "agent":
        from packages.core.models.document import Channel
        if await _rows(db, Channel, Channel.entity_id == workspace.entity_id, Channel.agent_id == row.id, or_(Channel.workspace_id.is_(None), Channel.workspace_id != workspace.id)):
            return True
        return bool(await _rows(db, AgentSubscription, AgentSubscription.agent_id == row.id, or_(AgentSubscription.workspace_id.is_(None), AgentSubscription.workspace_id != workspace.id)))
    if kind == "workflow":
        return bool(await _rows(db, WorkflowBinding, WorkflowBinding.workflow_id == row.id, or_(WorkflowBinding.workspace_id.is_(None), WorkflowBinding.workspace_id != workspace.id)))
    bindings = await _rows(db, AgentSkillBinding, AgentSkillBinding.skill_id == row.id)
    for binding in bindings:
        agent = await db.get(Agent, binding.agent_id)
        if agent is None or await _is_shared(db, workspace=workspace, kind="agent", row=agent):
            return True
    return False


async def _require_isolation_access(db, *, workspace, kind, row, actor, preserve_setup=True):
    """Read access permits a portable copy, never ownership of hidden setup."""
    from packages.core.models.permission import Capability

    await _require_component_access(db, row=row, kind=kind, entity_id=workspace.entity_id,
                                    actor=actor, capability=Capability.VIEW)
    if actor is None or not preserve_setup:
        return
    fields = {"agent": _AGENT_FIELDS, "skill": _SKILL_FIELDS, "workflow": _FLOW_FIELDS}[kind]
    copied = {field: deepcopy(getattr(row, field)) for field in fields}
    if "config" in copied:
        copied["config"] = {key: value for key, value in (copied["config"] or {}).items() if key not in _LOCAL_CONFIG_KEYS}
    protected = _portable(copied) != copied
    if kind == "agent":
        bindings = await _rows(db, AgentMCPBinding, AgentMCPBinding.agent_id == row.id)
        protected = protected or any(binding.config_override for binding in bindings)
        memories = await _rows(db, AgentMemory, AgentMemory.agent_id == row.id, AgentMemory.workspace_id == workspace.id,
                               AgentMemory.source == "blueprint", AgentMemory.user_id.is_(None))
        protected = protected or any(
            _portable(memory.content) != memory.content or _portable(memory.metadata_) != memory.metadata_
            for memory in memories
        )
    if protected:
        try:
            await _require_component_edit_access(db, row=row, kind=kind, entity_id=workspace.entity_id, actor=actor)
        except BlueprintUpgradeAccessDeniedError as exc:
            raise BlueprintUpgradeAccessDeniedError(
                f"Shared Blueprint {kind} {row.name!r} has private or local runtime setup. "
                "Ask its owner for edit access, or have the owner prepare a portable copy and reconfigure its setup before upgrading."
            ) from exc


async def _isolate(db, *, workspace, key, row, source_id, by_user_id):
    """Copy a reused definition and redirect only this Workspace's deployment."""
    kind = key.split(":", 1)[0]
    snapshot = _snapshot(row)
    values = deepcopy(snapshot["values"])
    values.update(workspace_id=workspace.id, visibility="workspace")
    if kind in {"agent", "skill"}:
        values.update(owner_user_id=by_user_id, is_public=False)
        # Credentials stay local. A clone can retain its parent's configured
        # overrides without ever putting those values into the public plan.
        values["config"] = deepcopy(row.config or {})
    clone = _MODELS[kind](id=generate_ulid(), **values)
    db.add(clone)
    await db.flush()
    if kind == "agent":
        for model in (AgentToolBinding, AgentSkillBinding, AgentMCPBinding):
            for binding in await _rows(db, model, model.agent_id == row.id):
                fields = {column.key: deepcopy(getattr(binding, column.key)) for column in model.__table__.columns if column.key not in {"id", "created_at", "updated_at"}}
                fields["agent_id"] = clone.id
                db.add(model(**fields))
        for memory in await _rows(db, AgentMemory, AgentMemory.agent_id == row.id, AgentMemory.workspace_id == workspace.id, AgentMemory.source == "blueprint", AgentMemory.user_id.is_(None)):
            fields = {field: deepcopy(getattr(memory, field)) for field in _JOURNAL_FIELDS["AgentMemory"]}
            fields["agent_id"] = clone.id
            db.add(AgentMemory(id=generate_ulid(), **fields))
        for subscription in await _rows(db, AgentSubscription, AgentSubscription.workspace_id == workspace.id, AgentSubscription.agent_id == row.id, lock=True):
            subscription.agent_id = clone.id
    elif kind == "skill":
        # Blueprint Agent binding reconciliation below installs the replacement
        # into its exact target Agents. Other consumers retain the old Skill.
        pass
    else:
        for binding in await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id, lock=True):
            if (binding.config or {}).get("source") == "blueprint":
                binding.workflow_id = clone.id
        for installation in await _rows(db, WorkflowTemplateInstallation, WorkflowTemplateInstallation.workflow_id == row.id, lock=True):
            if installation.template_id == blueprint_workflow_installation_source_id(source_id, workspace.id):
                installation.workflow_id = clone.id
    await _link(db, workspace=workspace, source_id=source_id, key=key, row=clone, by_user_id=by_user_id)
    return clone


async def _assign(db, row, values: dict, *, by_user_id=None):
    patch = {}
    for field, value in values.items():
        if field in {"config", "trigger_config", "metadata_"}:
            value = _replace_portable(getattr(row, field, None), value)
        if getattr(row, field, None) != value:
            patch[field] = deepcopy(value)
            setattr(row, field, deepcopy(value))
    if patch and hasattr(row, "revision"):
        await bump_revision(db, row, patch=patch, changed_by_kind="user" if by_user_id else "system", changed_by_id=by_user_id)


async def _retire(db, *, workspace, key, row, by_user_id):
    kind = key.split(":", 1)[0]
    shared = await _is_shared(db, workspace=workspace, kind=kind, row=row)
    if not shared:
        values = {"status": "inactive"}
        if kind == "workflow":
            values["is_active"] = False
        await _assign(db, row, values, by_user_id=by_user_id)
    if kind == "agent":
        for subscription in await _rows(db, AgentSubscription, AgentSubscription.workspace_id == workspace.id, AgentSubscription.agent_id == row.id, lock=True):
            subscription.status = "inactive"
    elif kind == "workflow":
        for binding in await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id, lock=True):
            if (binding.config or {}).get("source") == "blueprint":
                await _assign(db, binding, {"enabled": False, "status": "inactive"}, by_user_id=by_user_id)


async def apply_components(db, *, workspace, payload: dict, plan: dict, choices: dict | None = None, by_user_id=None, actor=None, effective_policy: WorkspacePolicy | None = None) -> dict:
    """Apply only reviewed rows. No commits, provider calls, or new Workspace."""
    await lock_reusable_resource_lifecycle(db, entity_id=workspace.entity_id)
    inventory = await _inventory(db, workspace=workspace, payload=payload, baseline=plan.get("current"), lock=True)
    fresh = await capture_components(db, workspace=workspace, payload=payload, baseline=plan.get("current"))
    choices = choices or {}
    selected = []
    kept = []
    for item in plan["items"]:
        if _hash(fresh.get(item["key"])) != item["expected_fingerprint"]:
            raise BlueprintUpgradePlanChangedError("Blueprint component changed after review; refresh the upgrade plan")
        decision = choices.get(item["key"])
        if isinstance(decision, dict):
            decision = decision.get("resolution")
        if decision not in {None, "keep_yours", "use_blueprint"}:
            raise BlueprintUpgradePlanChangedError("Unknown Blueprint component choice")
        if item["action"] == "keep_yours" and decision is None:
            raise BlueprintUpgradePlanChangedError("Review every component conflict before upgrading")
        if decision == "keep_yours":
            kept.append(item["key"])
        elif item["action"] != "unchanged":
            selected.append(item)
    # Permission checks for every changed existing component happen before any
    # row is written, including definitions that will be isolated or retired.
    selected_keys = {item["key"] for item in selected}
    # If a shared Skill or child Flow is replaced, its declared dependants need
    # the new local identity even when their portable source text is unchanged.
    # Respect explicit keep decisions; those components retain their old refs.
    changed_dependency_ids = {
        inventory[item["key"]].id for item in selected
        if item["new_content"] is not None
        and inventory.get(item["key"]) is not None
        and await _is_shared(db, workspace=workspace, kind=item["kind"], row=inventory[item["key"]])
    }
    if changed_dependency_ids:
        remaining = [item for item in plan["items"] if item["key"] not in selected_keys and item["key"] not in kept
                     and item["new_content"] is not None and item["kind"] in {"agent", "workflow"}
                     and item["key"] in inventory]
        while remaining:
            added = []
            for item in remaining:
                row = inventory[item["key"]]
                if item["kind"] == "agent":
                    dependencies = {binding.skill_id for binding in await _rows(db, AgentSkillBinding, AgentSkillBinding.agent_id == row.id)}
                else:
                    dependencies = set().union(*reusable_resource_ids_from_payload(row.steps))
                if not dependencies.intersection(changed_dependency_ids):
                    continue
                selected.append({**item, "_rebind_only": True})
                selected_keys.add(item["key"])
                added.append(item)
                if await _is_shared(db, workspace=workspace, kind=item["kind"], row=row):
                    changed_dependency_ids.add(row.id)
            if not added:
                break
            remaining = [item for item in remaining if item not in added]
    for item in selected:
        row = inventory.get(item["key"])
        if row is not None:
            if await _is_shared(db, workspace=workspace, kind=item["kind"], row=row):
                await _require_isolation_access(db, workspace=workspace, kind=item["kind"], row=row,
                                                actor=actor, preserve_setup=item["new_content"] is not None)
            else:
                await _require_component_edit_access(db, row=row, kind=item["kind"], entity_id=workspace.entity_id, actor=actor)
    before = await _journal_state(db, workspace=workspace, inventory=inventory)
    source_id, _ = _source(workspace, payload)
    source_version = str((payload.get("manifest") or {}).get("blueprint_version") or "1.0.0")
    specs = _specs(payload)
    policy = effective_policy or WorkspacePolicy()
    for item in selected:
        if item["kind"] == "agent" and item["new_content"] is not None and not item.get("_rebind_only"):
            spec = specs[item["key"]]
            _enforce_governance_against_agent(spec, policy)
    # Isolate reused resources before installing dependants. Their original
    # definitions and other Workspace deployments never receive a content write.
    replacements = {}
    for item in selected:
        row = inventory.get(item["key"])
        if row is not None and item["new_content"] is not None and await _is_shared(db, workspace=workspace, kind=item["kind"], row=row):
            inventory[item["key"]] = await _isolate(db, workspace=workspace, key=item["key"], row=row, source_id=source_id, by_user_id=by_user_id)
            replacements[row.id] = inventory[item["key"]].id
    # Allocate every missing Workflow first so subworkflow dependencies resolve
    # regardless of the author's ordering in recipe.workflows.
    for item in selected:
        if item["kind"] == "workflow" and inventory.get(item["key"]) is None and item["new_content"] is not None:
            spec = specs[item["key"]]
            placeholder = {**spec, "steps": [{"id": "start", "type": "trigger", "config": {}, "next": []}], "internal": True}
            wid = await _install_workflow(db, entity_id=workspace.entity_id, workspace_id=workspace.id, w=placeholder, source_template_id=blueprint_workflow_installation_source_id(source_id, workspace.id), source_version=source_version, installed_by=by_user_id, adopt_unmapped=False)
            inventory[item["key"]] = await db.get(WorkflowDefinition, wid)
    selected.sort(key=lambda item: ({"skill": 0, "workflow": 1, "agent": 2}[item["kind"]], item["key"]))
    for item in selected:
        key, kind = item["key"], item["kind"]
        row = inventory.get(key)
        if item.get("_rebind_only"):
            if kind == "agent":
                bindings = await _rows(db, AgentSkillBinding, AgentSkillBinding.agent_id == row.id, lock=True)
                by_skill = {binding.skill_id: binding for binding in bindings}
                for binding in bindings:
                    replacement = replacements.get(binding.skill_id)
                    if replacement:
                        if replacement in by_skill:
                            by_skill[replacement].status = binding.status
                            binding.status = "inactive"
                        else:
                            binding.skill_id = replacement
            elif kind == "workflow":
                def replace_references(value):
                    if isinstance(value, list):
                        return [replace_references(child) for child in value]
                    if isinstance(value, dict):
                        return {field: replacements.get(child, child) if field in {"workflow_id", "agent_id", "skill_id", "skill"} and isinstance(child, str) else replace_references(child) for field, child in value.items()}
                    return value
                rewritten = replace_references(row.steps)
                if rewritten != row.steps:
                    await _assign(db, row, {"steps": rewritten, "version": int(row.version or 1) + 1}, by_user_id=by_user_id)
            await _link(db, workspace=workspace, source_id=source_id, key=key, row=row, by_user_id=by_user_id)
            continue
        if item["new_content"] is None:
            if row is not None:
                await _retire(db, workspace=workspace, key=key, row=row, by_user_id=by_user_id)
            continue
        spec = specs[key]
        if kind == "skill":
            if row is None:
                sid = await _install_embedded_skill(db, entity_id=workspace.entity_id, workspace_id=workspace.id, source_blueprint_id=source_id, owner_user_id=by_user_id, sk=spec)
                row = inventory[key] = await db.get(Skill, sid)
            await _assign(db, row, _desired(kind, spec), by_user_id=by_user_id)
        elif kind == "workflow":
            flow_ids = {specs[k]["slug"]: r.id for k, r in inventory.items() if k.startswith("workflow:") and k in specs}
            flow_ids.update({k.split(":", 1)[1]: r.id for k, r in inventory.items() if k.startswith("workflow:")})
            runtime_spec = deepcopy(spec)
            runtime_spec["steps"] = WorkflowDependencyFactory.to_runtime(spec.get("steps") or [], workflow_id_by_key=flow_ids)
            values = _blueprint_workflow_definition_values(runtime_spec)
            changed_graph = row.steps != values["steps"] or row.variables != values["variables"]
            values["version"] = max(values["version"], int(row.version or 1) + int(changed_graph))
            await lock_reusable_resource_payload_references(db, entity_id=workspace.entity_id, payload=values["steps"])
            await _assign(db, row, values, by_user_id=by_user_id)
            if spec.get("internal") or spec.get("trigger_type") == "schedule":
                for binding in await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id, lock=True):
                    if (binding.config or {}).get("source") == "blueprint":
                        await _assign(db, binding, {"enabled": False, "status": "inactive"}, by_user_id=by_user_id)
            else:
                previous_bindings = await _rows(db, WorkflowBinding, WorkflowBinding.workspace_id == workspace.id, WorkflowBinding.workflow_id == row.id, lock=True)
                for previous in previous_bindings:
                    if (previous.config or {}).get("source") == "blueprint":
                        previous.config = {**dict(previous.config or {}), "source_template_id": source_id, "workspace_blueprint_workflow_slug": spec["slug"]}
                bid = await _install_workflow_binding(db, entity_id=workspace.entity_id, workspace_id=workspace.id, workflow_id=row.id, w=spec, source_template_id=source_id)
                binding = await db.get(WorkflowBinding, bid)
                desired_binding = _desired(kind, spec, current={"version": row.version})["binding"]
                desired_binding["config"] = {
                    **desired_binding["config"], "source": "blueprint",
                    "source_template_id": source_id, "workspace_blueprint_workflow_slug": spec["slug"],
                }
                # Installer merges for repeated installs; reviewed upgrades
                # replace portable fields so removed configuration is removed.
                await _assign(db, binding, desired_binding, by_user_id=by_user_id)
            mappings = await _rows(db, WorkflowTemplateInstallation, WorkflowTemplateInstallation.workflow_id == row.id, WorkflowTemplateInstallation.template_id == blueprint_workflow_installation_source_id(source_id, workspace.id), lock=True)
            if not mappings:
                db.add(WorkflowTemplateInstallation(id=generate_ulid(), entity_id=workspace.entity_id, template_id=blueprint_workflow_installation_source_id(source_id, workspace.id), component_key=spec["slug"], workflow_id=row.id, installed_version=source_version, installed_by=by_user_id, source_type="workspace_blueprint", installation_metadata={"source_workflow_key": spec["slug"], "internal": bool(spec.get("internal"))}))
            else:
                for mapping in mappings:
                    mapping.component_key = spec["slug"]
                    mapping.installed_version = source_version
                    mapping.installation_metadata = {"source_workflow_key": spec["slug"], "internal": bool(spec.get("internal"))}
        else:
            if row is not None:
                await _link(db, workspace=workspace, source_id=source_id, key=key, row=row, by_user_id=by_user_id)
            skill_ids = {specs[k]["slug"]: r.id for k, r in inventory.items() if k.startswith("skill:") and k in specs}
            required_refs: dict[str, list] = {}
            requires = (payload.get("contract") or {}).get("requires") or {}
            if requires.get("skills"):
                from packages.core.services.builtin_skill_loader import seed_builtin_skills
                await seed_builtin_skills(db)
            if spec.get("tool_bindings"):
                from packages.core.services.agent_service import ensure_runtime_tool_definitions
                await ensure_runtime_tool_definitions(db)
            for ref in requires.get("skills") or []:
                required_refs.setdefault(ref["slug"], []).append(ref)
            todos, requirements = [], []
            aid = await _install_embedded_agent(db, entity_id=workspace.entity_id, workspace_id=workspace.id, source_blueprint_id=source_id, owner_user_id=by_user_id, a=spec, skill_id_by_slug=skill_ids, required_skill_refs=required_refs, required_mcp_by_slug={r["slug"]: bool(r.get("required", True)) for r in requires.get("mcp_servers") or []}, live_setup_requirements=requirements, final_policy=policy, todos=todos)
            row = inventory[key] = await db.get(Agent, aid)
            desired = _desired(kind, spec)
            await _assign(db, row, {field: desired[field] for field in _AGENT_FIELDS}, by_user_id=by_user_id)
            allowed_tools = set(desired["tool_bindings"])
            for binding, name in (await db.execute(select(AgentToolBinding, ToolDefinition.name).join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id).where(AgentToolBinding.agent_id == row.id))).all():
                if name not in allowed_tools:
                    await db.delete(binding)
            allowed_skills = {r.id for r in inventory.values() if isinstance(r, Skill) and r.slug in desired["skill_bindings"]}
            # Installer receipts are the authoritative resolved external ids.
            allowed_skills.update(todo.payload["installed_skill_id"] for todo in requirements if "installed_skill_id" in todo.payload)
            for binding in await _rows(db, AgentSkillBinding, AgentSkillBinding.agent_id == row.id, lock=True):
                binding.status = "active" if binding.skill_id in allowed_skills else "inactive"
            allowed_mcp = {b["server_slug"] for b in desired["mcp_bindings"]}
            for binding, server_key in (await db.execute(select(AgentMCPBinding, MCPServer.server_key).join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id).where(AgentMCPBinding.agent_id == row.id))).all():
                if server_key not in allowed_mcp:
                    binding.status = "inactive"
            desired_memories = {(memory["memory_type"], memory["scope"], memory["content"]): memory for memory in desired["starter_memory"]}
            for memory in await _rows(db, AgentMemory, AgentMemory.agent_id == row.id, AgentMemory.workspace_id == workspace.id, AgentMemory.source == "blueprint", lock=True):
                desired_memory = desired_memories.get((memory.memory_type, memory.scope, memory.content))
                if desired_memory is None:
                    memory.status = "archived"
                else:
                    await _assign(db, memory, {**desired_memory, "status": "active"}, by_user_id=by_user_id)
            missing = [todo for todo in todos if todo.blocking and todo.kind in {"missing_skill", "mcp_server"}]
            if missing:
                raise BlueprintUpgradeIncompleteError("; ".join(todo.detail for todo in missing))
        await _link(db, workspace=workspace, source_id=source_id, key=key, row=row, by_user_id=by_user_id)
    await db.flush()
    actual = await capture_components(db, workspace=workspace, payload=payload, baseline=plan.get("current"))
    # A shared Skill replacement must also be adopted by otherwise unchanged
    # Blueprint Agents. Such reference changes are journalled in this transaction.
    for item in selected:
        if item["new_content"] is None or item.get("_rebind_only"):
            continue
        observed = (actual.get(item["key"]) or {}).get("content")
        expected = _desired(item["kind"], specs[item["key"]], current=observed)
        if observed != expected:
            raise BlueprintUpgradeIncompleteError(f"Blueprint component {item['name']!r} failed read-back verification")
    after_inventory = await _inventory(db, workspace=workspace, payload=payload, baseline=plan.get("current"))
    # Include original shared definitions in both snapshots. They are not a
    # removed resource just because this Workspace's mapping now points elsewhere.
    for old in plan.get("current", {}).values():
        if old.get("id") and old.get("kind") in _MODELS:
            old_row = await db.get(_MODELS[old["kind"]], old["id"])
            if old_row is not None and old_row.id not in {row.id for row in after_inventory.values()}:
                after_inventory[f"{old['kind']}:retained:{old_row.id}"] = old_row
    after = await _journal_state(db, workspace=workspace, inventory=after_inventory)
    journal = [{"before": before.get(key), "after": after.get(key)} for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    baseline = {
        item["key"]: {**(actual.get(item["key"]) or {"id": None, "kind": item["kind"], "slug": item["slug"], "content": None}), "source": _desired(item["kind"], specs[item["key"]], current=(actual.get(item["key"]) or {}).get("content")) if item["key"] in specs else None}
        for item in plan["items"]
    }
    return {"restore": journal, "baseline": baseline, "updated": [{"kind": i["kind"], "name": i["name"], "key": i["key"]} for i in selected], "kept": kept, "component_ids": {key: value["id"] for key, value in actual.items()}, "agent_replacements": {old: new for old, new in replacements.items() if any(item["kind"] == "agent" and inventory.get(item["key"]) is not None and inventory[item["key"]].id == new for item in selected)}}


async def validate_components_restore(db, *, workspace, restore: list, actor=None) -> None:
    await lock_reusable_resource_lifecycle(db, entity_id=workspace.entity_id)
    authorized = set()
    for entry in restore:
        snap = entry.get("after") or entry.get("before")
        model = _JOURNAL_MODELS[snap["model"]]
        row = await db.get(model, snap["identity"], populate_existing=True, with_for_update=True)
        if entry.get("after") is None:
            if row is not None:
                raise BlueprintUpgradePlanChangedError("Removed component binding was recreated after upgrade")
        elif row is None or _snapshot(row) != entry["after"]:
            raise BlueprintUpgradePlanChangedError("Blueprint component changed after upgrade; review before undoing")
        if row is not None and snap["model"] in {"Agent", "Skill", "WorkflowDefinition"}:
            kind = {"Agent": "agent", "Skill": "skill", "WorkflowDefinition": "workflow"}[snap["model"]]
            await _require_component_edit_access(db, row=row, kind=kind, entity_id=workspace.entity_id, actor=actor)
            authorized.add((kind, row.id))
        for state in (entry.get("before"), entry.get("after")):
            if not state:
                continue
            values = state["values"]
            parents = []
            if state["model"] in {"AgentToolBinding", "AgentSkillBinding", "AgentMCPBinding", "AgentMemory"} and values.get("agent_id"):
                parents.append(("agent", values["agent_id"]))
            if state["model"] == "WorkflowBinding":
                parents.append(("workflow", values["workflow_id"]))
            for kind, resource_id in parents:
                if (kind, resource_id) in authorized:
                    continue
                parent = await db.get(_MODELS[kind], resource_id, populate_existing=True, with_for_update=True)
                if parent is None or parent.entity_id != workspace.entity_id:
                    raise BlueprintUpgradePlanChangedError("Blueprint binding owner is no longer available")
                if state["model"] == "WorkflowBinding":
                    # Restoring this Workspace's deployment does not edit the
                    # referenced shared definition itself.
                    from packages.core.models.permission import Capability
                    await _require_component_access(db, row=parent, kind=kind, entity_id=workspace.entity_id,
                                                    actor=actor, capability=Capability.VIEW)
                else:
                    await _require_component_edit_access(db, row=parent, kind=kind, entity_id=workspace.entity_id, actor=actor)
                authorized.add((kind, resource_id))


async def revert_components(db, *, workspace, restore: list, actor=None, by_user_id=None, validated: bool = False) -> None:
    if not validated:
        await validate_components_restore(db, workspace=workspace, restore=restore, actor=actor)
    # Remove newly added mappings first to release unique identities before old
    # mappings return. Reusable definitions are retired, preserving any history.
    additions = [entry for entry in restore if entry.get("before") is None]
    for entry in reversed(additions):
        snap = entry["after"]
        row = await db.get(_JOURNAL_MODELS[snap["model"]], snap["identity"])
        if snap["model"] in {"Agent", "Skill", "WorkflowDefinition"}:
            await _assign(db, row, {"status": "inactive", **({"is_active": False} if snap["model"] == "WorkflowDefinition" else {})}, by_user_id=by_user_id)
        else:
            await db.delete(row)
    await db.flush()
    for entry in restore:
        snap = entry.get("before")
        if snap is None:
            continue
        model = _JOURNAL_MODELS[snap["model"]]
        row = await db.get(model, snap["identity"])
        if row is None:
            db.add(model(**{**deepcopy(snap["identity"]), **deepcopy(snap["values"])}))
        else:
            await _assign(db, row, snap["values"], by_user_id=by_user_id)
    await db.flush()
