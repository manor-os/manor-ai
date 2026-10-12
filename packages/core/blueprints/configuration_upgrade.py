"""Reviewed, transactional reconciliation of Blueprint-owned configuration.

The source and installed projections are separate: accepting an upstream
release while keeping a local customization must not ask the same question on
every preview. Runtime evidence is never part of either projection. Callers
own authorization of the Workspace, the surrounding transaction, and storage
of the returned baseline/restore receipt.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.exporter import (
    _RUNTIME_SETTINGS_KEYS,
    _without_nonportable_references,
)
from packages.core.models.base import generate_ulid
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.document import Document, DocumentChunk, DocumentGroup, DocumentGroupMember
from packages.core.models.goal import Goal
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_stat import WorkspaceStat


class ConfigurationUpgradeError(ValueError):
    """A reviewed configuration cannot be applied without a new decision."""


# These are deployment assets/operator consent, even when their portable
# defaults are advertised by a Blueprint. They must retain their local values.
_PROTECTED_SETTINGS = _RUNTIME_SETTINGS_KEYS | frozenset(
    {
        "blueprint_personalization",
        "workspace_shared_assets",
        "audio_defaults",
        "stickman_visual_continuity_profile",
        "access_mode",
        "frozen_at",
        "blueprint_frozen",
    }
)
_PROTECTED_MODEL_KEYS = frozenset(
    {
        "agents",
        "goals",
        "automated_runs_always_allow",
        "default_group_ids",
        "group_purposes",
        "workspace_shared_assets",
    }
)
_FIELDS = {
    "subscription": ("service_key", "custom_prompt", "config", "status"),
    "scheduled_job": (
        "name",
        "job_type",
        "schedule_kind",
        "cron_expr",
        "every_seconds",
        "run_at",
        "timezone",
        "payload_message",
        "execution_type",
        "execution_target",
        "execution_script",
        "default_delivery_mode",
        "enabled",
        "delete_after_run",
    ),
    "stat": (
        "key",
        "name",
        "description",
        "value_type",
        "unit",
        "window",
        "collector_type",
        "collector_config",
        "collection_cadence",
        "freshness_limit_seconds",
        "library_key",
        "goal_eligible",
        "status",
    ),
    "goal": (
        "goal_key",
        "title",
        "description",
        "metric_key",
        "target_value",
        "baseline_value",
        "deadline",
        "measurement_source",
        "measurement_cadence",
        "priority",
    ),
    "custom_field": (
        "name",
        "display_name",
        "field_type",
        "target",
        "options",
        "default_value",
        "required",
        "sort_order",
        "status",
    ),
}
_MODELS = {
    "subscription": AgentSubscription,
    "scheduled_job": ScheduledJob,
    "stat": WorkspaceStat,
    "goal": Goal,
    "custom_field": CustomFieldDefinition,
    "knowledge_pack": DocumentGroup,
    "knowledge_document": Document,
}
_ORDER = {
    "workspace_configuration": 0,
    "governance": 1,
    "stat": 2,
    "goal": 3,
    "subscription": 4,
    "scheduled_job": 5,
    "custom_field": 6,
    "knowledge_pack": 7,
    "knowledge_document": 8,
}


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return copy.deepcopy(value)


def configuration_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _current_fingerprint(content: Any, row: Any) -> str:
    return configuration_fingerprint({"content": content, "revision": getattr(row, "revision", None)})


def _safe(value: Any) -> Any:
    from packages.core.services.sensitive_data import sanitize_sensitive_payload

    value = _without_nonportable_references(_json(value))
    if sanitize_sensitive_payload(value) != value:
        raise ConfigurationUpgradeError(
            "Configuration contains sensitive values; use integration setup for credentials"
        )
    return value


def _merge_portable_dict(current: Any, desired: dict) -> dict:
    """Replace portable fields while retaining local references/credentials."""
    current = current if isinstance(current, dict) else {}
    portable = _without_nonportable_references(current)
    result = {key: copy.deepcopy(value) for key, value in current.items() if key not in portable}
    for key, value in desired.items():
        result[key] = _merge_portable_dict(current.get(key), value) if isinstance(value, dict) else copy.deepcopy(value)
    # A removed portable parent can still contain private deployment children.
    for key, value in current.items():
        if key not in desired and key in portable and isinstance(value, dict):
            retained = _merge_portable_dict(value, {})
            if retained:
                result[key] = retained
    return result


def _remap_runtime_references(value: Any, remaps: dict[str, dict[str, str]]) -> Any:
    fields = {"agent_id": "agents", "strategist_agent_id": "agents", "skill_id": "skills", "workflow_id": "workflows"}
    if isinstance(value, dict):
        return {
            key: remaps.get(fields[key], {}).get(item, item)
            if key in fields and isinstance(item, str)
            else _remap_runtime_references(item, remaps)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_remap_runtime_references(item, remaps) for item in value]
    return copy.deepcopy(value)


def _changed_leaf_paths(before: Any, after: Any, path: list[str]):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in after:
            yield from _changed_leaf_paths(before.get(key), after[key], [*path, key])
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for index, item in enumerate(after):
            yield from _changed_leaf_paths(before[index], item, [*path, index])
    elif before != after:
        yield path, before, after


def configuration_action(current: Any, source: Any, previous: dict | None) -> str:
    """Three-way comparison, including an acknowledged local customization."""
    if current == source:
        return "unchanged"
    if previous is not None and source == previous.get("source_content"):
        return "unchanged"
    if source is None:
        if previous is None:
            return "unchanged"
        return "remove" if current == previous.get("installed_content") else "keep_yours"
    if current is None:
        # A locally removed object is an edit, not a missing fresh component.
        return "keep_yours" if previous and previous.get("installed_content") is not None else "missing"
    if previous is not None and current == previous.get("source_content"):
        return "update"
    return "keep_yours"


def _flatten(value: dict, prefix: tuple[str, ...] = ()):
    for key, item in value.items():
        if key in _PROTECTED_MODEL_KEYS:
            continue
        path = (*prefix, key)
        if isinstance(item, dict):
            # Empty portable parents must not replace a local dictionary that
            # can contain deployment-only references or shared assets.
            yield from _flatten(item, path)
        else:
            yield path, item


def _read_path(workspace: Any, path: list[str]) -> Any:
    value = getattr(workspace, path[0], None)
    if len(path) == 1:
        return {"value": _safe(value)}
    for key in path[1:]:
        if isinstance(value, list) and isinstance(key, int) and 0 <= key < len(value):
            value = value[key]
            continue
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return {"value": _safe(value)}


def _write_path(workspace: Any, path: list[str], content: dict | None) -> None:
    if len(path) == 1:
        setattr(workspace, path[0], content.get("value") if content else None)
        return
    root = copy.deepcopy(getattr(workspace, path[0], None) or {})
    cursor = root
    for key in path[1:-1]:
        child = cursor[key] if isinstance(cursor, list) else cursor.get(key)
        cursor[key] = copy.deepcopy(child) if isinstance(child, (dict, list)) else {}
        cursor = cursor[key]
    if content is None:
        cursor.pop(path[-1], None)
    else:
        cursor[path[-1]] = copy.deepcopy(content["value"])
    setattr(workspace, path[0], root)


def _stat_source(spec: dict) -> dict:
    from packages.core.stats.library import get_library_entry

    source = {
        "key": str(spec.get("key") or spec.get("library_key") or "").lower(),
        "name": spec.get("name"),
        "description": spec.get("description"),
        "value_type": spec.get("value_type", "number"),
        "unit": spec.get("unit"),
        "window": spec.get("window", "latest"),
        "collector_type": spec.get("collector_type", "manual"),
        "collector_config": dict(spec.get("collector_config") or {}),
        "collection_cadence": spec.get("collection_cadence"),
        "freshness_limit_seconds": spec.get("freshness_limit_seconds"),
        "library_key": spec.get("library_key"),
        "goal_eligible": spec.get("goal_eligible", True),
        "status": "active",
    }
    if spec.get("library_key"):
        entry = get_library_entry(spec["library_key"])
        if entry is None:
            raise ConfigurationUpgradeError("Unknown Blueprint Stat library entry")
        source.update(
            {
                "name": spec.get("name") or entry.name,
                "description": entry.description,
                "value_type": entry.value_type,
                "unit": entry.unit,
                "window": spec.get("window") or entry.default_window,
                "collector_type": entry.collector_type,
                "collector_config": {**dict(entry.collector_config), **source["collector_config"]},
                "collection_cadence": spec.get("collection_cadence", entry.default_cadence),
                "freshness_limit_seconds": entry.freshness_limit_seconds,
                "goal_eligible": entry.goal_eligible,
            }
        )
        if entry.integration_key:
            from packages.core.stats.integration_keys import stat_integration_key_for_provider

            requested = source["collector_config"].get("integration_key")
            if requested and requested != entry.integration_key.value:
                raise ConfigurationUpgradeError("Library Stat requires its declared integration")
            provider = source["collector_config"].pop("provider", None)
            if provider and stat_integration_key_for_provider(provider) != entry.integration_key:
                raise ConfigurationUpgradeError("Library Stat provider does not match its declared integration")
            source["collector_config"]["integration_key"] = entry.integration_key.value
    source["key"] = str(source["key"]).strip().lower()
    source["name"] = str(source["name"] or source["key"]).strip()
    for field in ("description", "unit"):
        source[field] = str(source[field]).strip() if source[field] else None
    for field in ("value_type", "window", "collector_type"):
        source[field] = str(source[field]).strip().lower()
    source["collection_cadence"] = (
        str(source["collection_cadence"]).strip().lower() if source["collection_cadence"] else None
    )
    return _safe(source)


def _goal_source(spec: dict, workspace_id: str) -> dict:
    from packages.core.goals.scheduling import (
        default_workspace_measurement_source,
        is_workspace_internal_measurement_source,
    )

    source = {key: spec.get(key) for key in _FIELDS["goal"]}
    source.update(
        {
            "stat_key": spec.get("stat_key"),
            "retired": False,
            "metric_key": spec.get("metric_key") or spec.get("stat_key") or "completion",
            "priority": spec.get("priority", 3),
        }
    )
    if source["stat_key"]:
        source["measurement_source"] = None
        source["measurement_cadence"] = None
    else:
        source["measurement_source"] = default_workspace_measurement_source(
            source["measurement_source"], workspace_id=workspace_id
        )
        if is_workspace_internal_measurement_source(source["measurement_source"]):
            source["measurement_cadence"] = source["measurement_cadence"] or "daily"
            if source["baseline_value"] is None:
                source["baseline_value"] = 0
    return _safe(source)


def _desired_governance(payload: dict, settings: dict | None = None) -> tuple[list[dict], Any]:
    from packages.core.governance.approval_projection import import_blueprint_governance
    from packages.core.governance.defaults import normalize_default_workspace_governance
    from packages.core.governance.presets import apply_preset

    record = (settings or {}).get("_blueprint") or {}
    preserve = bool(record.get("author_hard_blocks_installed", False))
    policy, rules = import_blueprint_governance(
        (payload.get("policy") or {}).get("governance") or {},
        ((payload.get("recipe") or {}).get("operating_model") or {}).get("rules") or [],
        preserve_hard_blocks=preserve,
    )
    rules, policy, _ = normalize_default_workspace_governance(rules, policy, preserve_hard_blocks=preserve)
    policy = apply_preset(policy, record.get("governance_preset") or "follow_default")
    return rules, policy


def _governance_content(raw: dict | None) -> dict:
    """Private upgrade snapshots omit only the nonportable account binding.

    Do not run these through the public export sanitizer: user resource rules
    need their exact account/target IDs for concurrency checks and undo.
    """
    from packages.core.governance.approval_projection import policy_override
    from packages.core.governance.policy import policy_from_dict, policy_to_dict

    policy = policy_from_dict(raw)
    result = policy_to_dict(policy)
    result["approval"] = policy_override(policy).model_dump(mode="json")
    result["approval"]["rules"].sort(key=lambda rule: rule["id"])
    return result


def _merge_governance_upgrade(current: dict, source: dict, previous_source: dict | None) -> dict:
    """Update proven template configuration while retaining operator choices."""
    current, source = _governance_content(current), _governance_content(source)
    previous = _governance_content(previous_source) if previous_source is not None else None
    previous_rules = {
        rule["id"]: rule for rule in (previous or {}).get("approval", {}).get("rules", [])
        if rule["source_kind"] == "user"
    }
    # A manually added or edited rule remains local even if its ID originally
    # came from a template. Only unchanged, baseline-proven author rules can
    # be replaced or removed by an upgrade.
    local_rules = [
        rule for rule in current["approval"]["rules"]
        if rule["source_kind"] == "user" and previous_rules.get(rule["id"]) != rule
    ]
    local_ids = {rule["id"] for rule in local_rules}
    incoming = [rule for rule in source["approval"]["rules"] if rule["id"] not in local_ids]
    source["approval"] = {
        "mode": current["approval"]["mode"],
        "rules": [*local_rules, *incoming],
    }
    # Validate the merged rule limit and source contracts before preview/apply.
    return _governance_content(source)


def _governance_previous(entry: dict | None) -> dict | None:
    if entry is None:
        return None
    source = entry.get("blueprint_content", entry.get("source_content"))
    return _governance_content(source) if source is not None else None


def _targets(payload: dict, workspace_id: str, settings: dict | None = None) -> dict[str, dict]:
    recipe = payload.get("recipe") or {}
    output: dict[str, dict] = {}

    def add(kind: str, identity: str, content: Any, **extra: Any) -> None:
        key = f"{kind}:{identity}"
        if key in output:
            raise ConfigurationUpgradeError(f"Ambiguous Blueprint configuration key {key}")
        output[key] = {"key": key, "kind": kind, "identity": identity, "source_content": _safe(content), **extra}

    om = copy.deepcopy(recipe.get("operating_model") or {})
    # Exported operating governance is a mirror of policy.governance. The
    # governance target below owns its account binding, rule merge and undo;
    # flattening this portable mirror would write unbound approval leaves.
    om.pop("governance", None)
    # App keys and targets are one reference-bearing configuration. Flattening
    # them would write portable slugs directly into runtime binding fields.
    if "app_view" in om:
        from packages.core.services.workspace_app_view import normalize_app_view_config
        add("workspace_configuration", "operating_model/app_view",
            {"value": normalize_app_view_config(om.pop("app_view"))}, path=["operating_model", "app_view"])
    operating_rules, desired_policy = _desired_governance(payload, settings)
    if "rules" in om:
        om["rules"] = operating_rules
    settings = dict(om.pop("settings", None) or {})
    shell = {
        "kind": (payload.get("manifest") or {}).get("kind") or om.pop("kind", None) or None,
        "operating_context": om.pop("context", None) or None,
        "primary_work": om.pop("primary_work", None) or None,
        "heartbeat_enabled": bool(om.pop("heartbeat_enabled", False)),
        "heartbeat_cadence": om.pop("heartbeat_cadence", None),
    }
    for field, value in shell.items():
        add("workspace_configuration", field, {"value": value}, path=[field])
    strategist = dict(recipe.get("strategist") or {})
    if strategist:
        cadence = strategist.pop("cadence", None)
        if isinstance(cadence, dict):
            if cadence.get("schedule"):
                strategist["cadence"] = cadence["schedule"]
            if cadence.get("trigger_conditions") is not None:
                strategist["trigger_conditions"] = cadence["trigger_conditions"]
        elif cadence is not None:
            strategist["cadence"] = cadence
        om["strategist"] = {**dict(om.get("strategist") or {}), **strategist}
    if recipe.get("prompts"):
        om["blueprint_prompts"] = recipe["prompts"]
    if recipe.get("escalation_rules"):
        om["blueprint_governance_rules"] = recipe["escalation_rules"]
    for prefix, values in (
        ("operating_model", om),
        ("settings", {k: v for k, v in settings.items() if k not in _PROTECTED_SETTINGS}),
    ):
        for path, value in _flatten(_safe(values)):
            add("workspace_configuration", "/".join((prefix, *path)), {"value": value}, path=[prefix, *path])

    for spec in recipe.get("subscriptions") or []:
        source = {
            "service_key": spec["service_key"],
            "agent_slug": spec.get("agent_slug"),
            "marketplace_agent_id": spec.get("marketplace_agent_id"),
            "custom_prompt": spec.get("custom_prompt"),
            "config": dict(spec.get("config") or {}),
            "status": "active",
        }
        add("subscription", str(spec["service_key"]), source)
    for spec in recipe.get("stats") or []:
        source = _stat_source(spec)
        add("stat", source["key"], source)
    from packages.core.goals.factory import GoalIdentityFactory

    for spec in GoalIdentityFactory.normalize_records(recipe.get("goals") or []):
        source = _goal_source(spec, workspace_id)
        add("goal", str(source["goal_key"]), source)
    for spec in recipe.get("scheduled_jobs") or []:
        source = {key: spec.get(key) for key in _FIELDS["scheduled_job"]}
        source.update(
            {
                "job_type": spec.get("job_type", "cron"),
                "timezone": spec.get("timezone", "UTC"),
                "execution_type": spec.get("execution_type") or "agent",
                "execution_target": dict(spec.get("execution_target") or {}),
                "enabled": bool(spec.get("enabled", True)),
                "delete_after_run": bool(spec.get("delete_after_run", False)),
            }
        )
        if source["execution_type"] == "agent_message":
            source["execution_type"] = "agent"
        add("scheduled_job", str(spec["job_id"]), source)
    for spec in recipe.get("custom_fields") or []:
        source = {
            "name": spec["name"],
            "display_name": spec.get("display_name") or spec["name"],
            "field_type": spec.get("field_type", "text"),
            "target": spec.get("target", "task"),
            "options": list(spec.get("options") or []),
            "default_value": spec.get("default_value"),
            "required": bool(spec.get("required", False)),
            "sort_order": int(spec.get("sort_order", 0)),
            "status": "active",
        }
        add("custom_field", f"{source['target']}/{source['name']}", source)
    from packages.core.governance.policy import policy_to_dict

    policy = policy_to_dict(desired_policy)
    add("governance", "policy", _governance_content(policy))
    for pack in (payload.get("embedded") or {}).get("knowledge_packs") or []:
        slug = str(pack["slug"])
        source = {
            "slug": slug,
            "title": pack.get("title") or slug,
            "purpose": pack.get("purpose"),
            "folder_structure": list(pack.get("folder_structure") or []),
            "mode": pack.get("mode") or "skeleton",
            "external_source": pack.get("external_source"),
            "retired": False,
        }
        add("knowledge_pack", slug, source)
        if source["mode"] == "inline_text":
            for document in pack.get("starter_documents") or []:
                identity = str(document.get("key") or document["path"])
                from packages.core.blueprints.installer import _blueprint_document_template

                add(
                    "knowledge_document",
                    f"{slug}/{identity}",
                    {
                        "path": document["path"],
                        "body_md": document.get("body_md") or "",
                        "key": document.get("key"),
                        "template": _blueprint_document_template(document),
                    },
                    pack_slug=slug,
                )
    return output


async def _row(db: AsyncSession, workspace: Any, target: dict, *, lock: bool = False) -> Any:
    kind, identity = target["kind"], target["identity"]
    if kind not in _MODELS:
        return None
    model = _MODELS[kind]
    query = select(model).where(model.entity_id == workspace.entity_id)
    if kind == "knowledge_pack" and target.get("row_id"):
        query = query.where(
            or_(
                model.workspace_id == workspace.id,
                model.settings["blueprint_detached_workspace_id"].astext == workspace.id,
            )
        )
    elif kind != "knowledge_document":
        query = query.where(model.workspace_id == workspace.id)
    if target.get("row_id"):
        query = query.where(model.id == target["row_id"])
    elif kind == "subscription":
        query = query.where(model.service_key == identity)
    elif kind == "scheduled_job":
        from packages.core.constants.blueprints import installed_blueprint_job_id

        query = query.where(
            model.job_id == installed_blueprint_job_id(identity, workspace.id), model.agent_initiative_id.is_(None)
        )
    elif kind == "stat":
        query = query.where(model.key == identity)
    elif kind == "goal":
        query = query.where(model.goal_key == identity)
    elif kind == "custom_field":
        field_target, name = identity.split("/", 1)
        query = query.where(model.target == field_target, model.name == name)
    elif kind == "knowledge_pack":
        source = target.get("source_content") or target.get("installed_content") or {}
        query = query.where(
            or_(model.settings["installed_from_blueprint_slug"].astext == identity, model.name == source.get("title"))
        )
    elif kind == "knowledge_document":
        source = target.get("source_content") or target.get("installed_content") or {}
        from packages.core.blueprints.installer import _blueprint_starter_document_predicate

        pack = await _row(
            db, workspace, {"kind": "knowledge_pack", "identity": target["pack_slug"], "source_content": {}}
        )
        if pack is None:
            return None
        query = query.join(DocumentGroupMember, DocumentGroupMember.document_id == model.id).where(
            DocumentGroupMember.group_id == pack.id,
            _blueprint_starter_document_predicate(source, target["pack_slug"]),
        )
    if lock:
        query = query.with_for_update()
    rows = list((await db.execute(query.execution_options(populate_existing=True))).scalars().all())
    if len(rows) > 1:
        raise ConfigurationUpgradeError(f"Configuration identity {target['key']} is ambiguous")
    return rows[0] if rows else None


async def _read(db: AsyncSession, workspace: Any, target: dict, *, lock: bool = False) -> tuple[Any, Any]:
    kind = target["kind"]
    if kind == "workspace_configuration":
        if target["path"] == ["operating_model", "app_view"]:
            value = (workspace.operating_model or {}).get("app_view")
            if value is None:
                return None, None
            from packages.core.blueprints.app_view import remap_workspace_app_view
            return {"value": await remap_workspace_app_view(db, workspace, value, to_runtime=False)}, None
        return _read_path(workspace, target["path"]), None
    if kind == "governance":
        from packages.core.governance.policy import policy_from_dict, policy_to_dict
        from packages.core.models.governance import GovernancePolicy

        query = select(GovernancePolicy).where(
            GovernancePolicy.workspace_id == workspace.id, GovernancePolicy.entity_id == workspace.entity_id
        )
        if lock:
            query = query.with_for_update()
        row = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
        from packages.core.governance.approval_policy import ApprovalPolicyBinding

        if row is None:
            raise ConfigurationUpgradeError("Workspace approval configuration is missing; complete its migration first")
        policy = policy_from_dict(row.policy)
        if not isinstance(policy.approval, ApprovalPolicyBinding) or policy.approval.root_entity_id != workspace.entity_id:
            raise ConfigurationUpgradeError("Workspace approval account binding is missing or does not match")
        return _governance_content(policy_to_dict(policy)), row
    row = await _row(db, workspace, target, lock=lock)
    if row is None:
        return None, None
    target["row_id"] = row.id
    if kind in _FIELDS:
        content = {key: getattr(row, key) for key in _FIELDS[kind]}
        if kind == "subscription":
            agent = await db.get(Agent, row.agent_id)
            if agent is None or (agent.entity_id is not None and agent.entity_id != workspace.entity_id):
                raise ConfigurationUpgradeError("Subscription Agent is unavailable")
            config = dict(agent.config or {})
            expected = target.get("source_content") or target.get("installed_content") or {}
            marketplace_reference = bool(expected.get("marketplace_agent_id"))
            content["agent_slug"] = None if marketplace_reference else agent.slug
            content["marketplace_agent_id"] = (
                (config.get("source_agent_id") or agent.id) if marketplace_reference else None
            )
        if kind == "goal":
            stat = await db.get(WorkspaceStat, row.stat_id) if row.stat_id else None
            if stat and (stat.entity_id != workspace.entity_id or stat.workspace_id != workspace.id):
                raise ConfigurationUpgradeError("Goal Stat belongs to another Workspace")
            content["stat_key"] = stat.key if stat else None
            # Completion/abandonment are lifecycle history, not a portable
            # definition conflict. Never reactivate either during an upgrade.
            content["retired"] = row.status == "paused"
        return _safe(content), row
    if kind == "knowledge_pack":
        if row.workspace_id != workspace.id:
            return None, row
        settings = dict(row.settings or {})
        return _safe(
            {
                "slug": settings.get("installed_from_blueprint_slug") or target["identity"],
                "title": row.name,
                "purpose": settings.get("purpose"),
                "folder_structure": settings.get("folder_structure") or [],
                "mode": settings.get("mode") or "skeleton",
                "external_source": settings.get("external_source"),
                "retired": bool(settings.get("blueprint_retired")),
            }
        ), row
    pack = await _row(db, workspace, {"kind": "knowledge_pack", "identity": target["pack_slug"], "source_content": {}})
    membership = await db.get(DocumentGroupMember, (row.id, pack.id)) if pack else None
    if membership is None or row.is_trashed:
        return None, row
    from packages.core.services.document_access import document_workspace_ids

    owners = await document_workspace_ids(db, row)
    if workspace.id not in owners:
        raise ConfigurationUpgradeError("Starter document no longer belongs to this Workspace")
    shared_group = (
        await db.execute(
            select(DocumentGroupMember.group_id)
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(DocumentGroupMember.document_id == row.id, DocumentGroup.workspace_id.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if owners != {workspace.id} or shared_group is not None:
        target["shared_document"] = True
    if row.fs_path or row.file_url:
        target["body_read_only"] = True
    return _document_content(row), row


def _document_content(row: Document) -> dict:
    from packages.core.services.document_service import _metadata_text_content

    metadata = row.metadata_ or {}
    starter_path = metadata.get("blueprint_starter_path")
    path = starter_path if isinstance(starter_path, str) and starter_path else row.name
    # Installed starter documents store only the basename in Document.name;
    # their portable, pack-relative path lives in Blueprint provenance.
    if row.name not in (path, path.rsplit("/", 1)[-1]):
        path = row.name
    return _safe(
        {
            "path": path,
            "body_md": _metadata_text_content(row) or "",
            "key": metadata.get("blueprint_document_key"),
            "template": metadata.get("blueprint_template"),
        }
    )


async def _lock_workspace(db: AsyncSession, workspace: Any) -> Workspace:
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle

    await lock_reusable_resource_lifecycle(db, entity_id=workspace.entity_id)
    row = (
        await db.execute(
            select(Workspace)
            .where(Workspace.id == workspace.id, Workspace.entity_id == workspace.entity_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.deleted_at is not None:
        raise ConfigurationUpgradeError("Workspace is unavailable")
    return row


async def capture_configuration(db: AsyncSession, *, workspace: Any, payload: dict) -> dict:
    entries = _targets(payload, workspace.id, workspace.settings)
    for target in entries.values():
        current, row = await _read(db, workspace, target)
        target["installed_content"] = current
    return {"version": 1, "entries": entries}


async def plan_configuration(
    db: AsyncSession, *, workspace: Any, payload: dict, baseline: dict | None = None, actor: Any = None
) -> dict:
    entries = _targets(payload, workspace.id, workspace.settings)
    # Old baselines also tracked the mirror's leaves. They must not become
    # retirement writes when the mirror stops being an independent target.
    previous = {
        key: entry for key, entry in ((baseline or {}).get("entries") or {}).items()
        if not (entry.get("kind") == "workspace_configuration"
                and (entry.get("path") or [])[:2] == ["operating_model", "governance"])
    }
    for key, entry in previous.items():
        if key not in entries:
            entries[key] = {**copy.deepcopy(entry), "source_content": None}
        elif entry.get("row_id"):
            entries[key]["row_id"] = entry["row_id"]
    items = []
    for key, target in entries.items():
        current, row = await _read(db, workspace, target)
        if row is not None and target["kind"] == "knowledge_document":
            from packages.core.blueprints.upgrade import _require_document_access
            from packages.core.models.permission import Capability

            await _require_document_access(db, row=row, actor=actor, capability=Capability.VIEW)
        comparison_previous = previous.get(key)
        if target["kind"] == "governance":
            source = _governance_content(target["source_content"])
            previous_source = _governance_previous(comparison_previous)
            target["blueprint_content"] = source
            target["source_content"] = _merge_governance_upgrade(current, source, previous_source)
            if comparison_previous is not None:
                comparison_previous = {
                    "source_content": _merge_governance_upgrade(current, previous_source, previous_source),
                    "installed_content": _governance_content(comparison_previous.get("installed_content")),
                }
        action = configuration_action(current, target["source_content"], comparison_previous)
        item = {
            "key": key,
            "kind": target["kind"],
            "slug": target["identity"],
            "name": target["identity"],
            "action": action,
            "conflict": action == "keep_yours",
            "current_content": current,
            "new_content": target["source_content"],
            "baseline_content": (previous.get(key) or {}).get("source_content"),
            "baseline_known": key in previous,
            "expected_fingerprint": _current_fingerprint(current, row),
            "changes": ["retire Blueprint configuration; preserve history"]
            if target["source_content"] is None
            else ["reconcile Workspace configuration"],
            "customized": action == "unchanged" and current != target["source_content"],
        }
        if target.get("body_read_only") and action != "unchanged" and target["source_content"] is not None:
            item["blocked_reason"] = (
                "This starter document is now a stored file; keep it or reconcile it through the document editor"
            )
        if target.get("shared_document") and action != "unchanged" and target["source_content"] is not None:
            item["blocked_reason"] = (
                "This starter document is shared; keep its content or create a Workspace-owned copy"
            )
        items.append(item)
    return {"items": items, "targets": entries, "baseline": copy.deepcopy(baseline)}


async def effective_configuration_policy(
    db: AsyncSession, *, workspace: Any, payload: dict, plan: dict, choices: dict[str, str] | None = None
) -> Any:
    """Policy components must satisfy after the reviewed configuration choices."""
    from packages.core.governance.policy import policy_from_dict
    from packages.core.governance.service import get_policy

    item = next((item for item in plan.get("items") or [] if item["kind"] == "governance"), None)
    if item is None:
        return await get_policy(db, workspace.id)
    return policy_from_dict(_selected_governance_content(workspace, plan, choices or {}))


def _selected_governance_content(workspace: Any, plan: dict, choices: dict[str, str]) -> dict:
    """Derived approval rules follow the selected operating-model source.

    Governance and source configuration have separate review choices. Keeping
    local hard limits must not retain a stale compiled business rule, and
    keeping local business rules must not install the template's projection.
    """
    from packages.core.governance.approval_projection import replace_workspace_rules
    from packages.core.governance.policy import policy_from_dict, policy_to_dict

    def selected(item: dict) -> Any:
        choice = choices.get(item["key"])
        if item["action"] == "keep_yours" and choice is None:
            raise ConfigurationUpgradeError("Review every configuration conflict before upgrading")
        if choice == "keep_yours" or item["action"] == "unchanged":
            return item["current_content"]
        return item["new_content"]

    governance = next(item for item in plan["items"] if item["kind"] == "governance")
    content = selected(governance)
    rules_item = next((item for item in plan["items"] if (
        plan["targets"][item["key"]].get("path") == ["operating_model", "rules"]
    )), None)
    rules = (workspace.operating_model or {}).get("rules") or []
    if rules_item is not None:
        rules = (selected(rules_item) or {}).get("value") or []
    projected = replace_workspace_rules(policy_from_dict(content), rules)
    return _governance_content(policy_to_dict(projected))


async def _resolve_agent(
    db: AsyncSession,
    workspace: Any,
    source: dict,
    payload: dict,
    *,
    by_user_id: str | None = None,
    restore_agent_id: str | None = None,
) -> Agent:
    from packages.core.blueprints.upgrade import _installed_blueprint_component, _workflow_source_template_ids

    if restore_agent_id:
        agent = await db.get(Agent, restore_agent_id)
        if agent is None or agent.entity_id != workspace.entity_id or agent.deleted_at is not None:
            raise ConfigurationUpgradeError("The former subscription Agent is unavailable")
        return agent
    if source.get("marketplace_agent_id"):
        from packages.core.services.marketplace_agent_service import ensure_marketplace_agent_installed

        return await ensure_marketplace_agent_installed(
            db, entity_id=workspace.entity_id, agent_id=source["marketplace_agent_id"], owner_user_id=by_user_id
        )
    record = (workspace.settings or {}).get("_blueprint") or {}
    for spec in (payload.get("embedded") or {}).get("agents") or []:
        if spec.get("slug") == source.get("agent_slug"):
            agent = await _installed_blueprint_component(
                db,
                workspace=workspace,
                source_blueprint_ids=_workflow_source_template_ids(record, payload, record.get("blueprint_id")),
                kind="agent",
                spec=spec,
            )
            if agent is not None:
                return agent
    conditions = [Agent.entity_id == workspace.entity_id, Agent.deleted_at.is_(None), Agent.status == "active"]
    if source.get("marketplace_agent_id"):
        conditions.append(
            or_(
                Agent.id == source["marketplace_agent_id"],
                Agent.config["source_agent_id"].astext == source["marketplace_agent_id"],
            )
        )
    else:
        conditions.extend(
            [
                Agent.workspace_id == workspace.id,
                or_(
                    Agent.slug == source.get("agent_slug"),
                    Agent.config["source_blueprint_component_key"].astext == source.get("agent_slug"),
                ),
            ]
        )
    rows = list((await db.execute(select(Agent).where(*conditions))).scalars().all())
    if len(rows) != 1:
        raise ConfigurationUpgradeError("Blueprint subscription requires one available installed Agent")
    return rows[0]


async def _job_fields(
    db: AsyncSession, workspace: Any, source: dict, payload: dict, *, actor: Any = None, by_user_id: str | None = None
) -> dict:
    from packages.core.blueprints.upgrade import _installed_blueprint_component, _workflow_source_template_ids
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowTemplateInstallation

    record = (workspace.settings or {}).get("_blueprint") or {}
    values = copy.deepcopy(source)
    target = dict(source.get("execution_target") or {})
    execution_type = source.get("execution_type") or "agent"
    values["agent_id"] = None
    if execution_type == "agent":
        rows = list(
            (
                await db.execute(
                    select(AgentSubscription).where(
                        AgentSubscription.entity_id == workspace.entity_id,
                        AgentSubscription.workspace_id == workspace.id,
                        AgentSubscription.service_key == target.get("service_key"),
                        AgentSubscription.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != 1:
            raise ConfigurationUpgradeError("Scheduled Agent requires one active Workspace subscription")
        values["agent_id"] = rows[0].agent_id
    elif execution_type == "skill":
        spec = next(
            (
                item
                for item in (payload.get("embedded") or {}).get("skills") or []
                if (item.get("component_id") or item.get("id") or item.get("slug")) == target.get("skill_component_key")
            ),
            None,
        )
        skill = None
        if spec is not None:
            skill = await _installed_blueprint_component(
                db,
                workspace=workspace,
                source_blueprint_ids=_workflow_source_template_ids(record, payload, record.get("blueprint_id")),
                kind="skill",
                spec=spec,
            )
        elif target.get("skill_marketplace_id"):
            marketplace_source = target.get("skill_marketplace_source") or "platform"
            if marketplace_source == "platform":
                from packages.core.services.marketplace_skill_service import ensure_marketplace_skill_installed

                skill = await ensure_marketplace_skill_installed(
                    db, entity_id=workspace.entity_id, skill_id=target["skill_marketplace_id"], owner_user_id=by_user_id
                )
            elif marketplace_source == "manor":
                if skill is None:
                    raise ConfigurationUpgradeError("The exact Manor Marketplace Skill is unavailable")
        elif target.get("skill_component_key"):
            from packages.core.models.skill import Skill

            candidates = list(
                (
                    await db.execute(
                        select(Skill).where(
                            Skill.entity_id == workspace.entity_id,
                            Skill.slug == target["skill_component_key"],
                            Skill.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(candidates) == 1:
                skill = candidates[0]
        if skill is None:
            raise ConfigurationUpgradeError("Scheduled Skill is unavailable")
        from packages.core.blueprints.upgrade import _require_component_access
        from packages.core.models.permission import Capability

        await _require_component_access(
            db, row=skill, kind="skill", entity_id=workspace.entity_id, actor=actor, capability=Capability.VIEW
        )
        target.update({"skill_id": skill.id, "workspace_id": workspace.id})
    elif execution_type == "workflow":
        from packages.core.blueprints.installer import blueprint_workflow_installation_source_id

        source_ids = _workflow_source_template_ids(record, payload, record.get("blueprint_id"))
        source_ids += [blueprint_workflow_installation_source_id(value, workspace.id) for value in list(source_ids)]
        rows = list(
            (
                await db.execute(
                    select(WorkflowDefinition)
                    .join(
                        WorkflowTemplateInstallation, WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id
                    )
                    .where(
                        WorkflowDefinition.entity_id == workspace.entity_id,
                        WorkflowDefinition.workspace_id == workspace.id,
                        WorkflowTemplateInstallation.template_id.in_(source_ids),
                        WorkflowTemplateInstallation.component_key == target.get("workflow_slug"),
                    )
                )
            )
            .scalars()
            .unique()
            .all()
        )
        if len(rows) != 1:
            raise ConfigurationUpgradeError("Scheduled Flow requires one installed Workspace Flow")
        flow = rows[0]
        target.update({"workflow_id": flow.id, "workspace_id": workspace.id})
        spec = next(
            (
                item
                for item in (payload.get("recipe") or {}).get("workflows") or []
                if item.get("slug") == target.get("workflow_slug")
            ),
            {},
        )
        if not spec.get("internal"):
            bindings = list(
                (
                    await db.execute(
                        select(WorkflowBinding).where(
                            WorkflowBinding.workspace_id == workspace.id,
                            WorkflowBinding.workflow_id == flow.id,
                            WorkflowBinding.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(bindings) != 1 or (source.get("enabled") and not bindings[0].enabled):
                raise ConfigurationUpgradeError("Scheduled Flow binding is unavailable or ambiguous")
            target["binding_id"] = bindings[0].id
    elif execution_type == "strategist_review":
        target["workspace_id"] = workspace.id
    from packages.core.services.reusable_resource_locks import (
        lock_reusable_resource_references,
        reusable_resource_ids_from_payload,
    )

    agent_ids, skill_ids, workflow_ids = reusable_resource_ids_from_payload(target)
    if values["agent_id"]:
        agent_ids.add(values["agent_id"])
    await lock_reusable_resource_references(
        db, entity_id=workspace.entity_id, agent_ids=agent_ids, skill_ids=skill_ids, workflow_ids=workflow_ids
    )
    values["execution_target"] = target
    return values


async def _restored_job_fields(db: AsyncSession, workspace: Any, content: dict, references: dict) -> dict:
    """Restore local links from the server receipt, never from a client payload."""
    from packages.core.models.skill import Skill
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    target = dict(references.get("execution_target") or {})
    for field, model in (
        ("skill_id", Skill),
        ("workflow_id", WorkflowDefinition),
        ("binding_id", WorkflowBinding),
        ("stat_id", WorkspaceStat),
        ("goal_id", Goal),
    ):
        if not target.get(field):
            continue
        row = await db.get(model, target[field])
        if (
            row is None
            or row.entity_id != workspace.entity_id
            or (getattr(row, "workspace_id", None) not in {None, workspace.id})
        ):
            raise ConfigurationUpgradeError("A former schedule dependency is unavailable")
    agent_id = references.get("agent_id")
    if agent_id:
        agent = await db.get(Agent, agent_id)
        if agent is None or agent.entity_id != workspace.entity_id:
            raise ConfigurationUpgradeError("A former schedule Agent is unavailable")
    return {**copy.deepcopy(content), "execution_target": target, "agent_id": agent_id}


def _is_retired(kind: str, content: Any) -> bool:
    if content is None:
        return True
    if kind in {"subscription", "stat", "custom_field"}:
        return content.get("status") == "inactive"
    if kind == "scheduled_job":
        return content.get("enabled") is False
    if kind == "goal":
        # Achieved Goals remain terminal; removal never rewrites history.
        return bool(content.get("retired"))
    return False


async def _sync_collection(db: AsyncSession, workspace: Any, kind: str, row: Any) -> None:
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory

    await db.flush()
    if kind == "goal":
        from packages.core.goals.scheduling import install_measurement_schedule, should_install_measurement_schedule

        should_run = should_install_measurement_schedule(row)
        job_id = f"gm:{row.id}"
    else:
        from packages.core.stats.scheduling import should_schedule, sync_stat_collection_schedule

        should_run = should_schedule(row)
        job_id = f"wsstat:{row.id}"
    if workspace.status != "active" or not workspace.heartbeat_enabled:
        should_run = False
    if should_run:
        if kind == "goal":
            await install_measurement_schedule(db, row)
        else:
            await sync_stat_collection_schedule(db, row)
    else:
        job = (
            await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.entity_id == workspace.entity_id,
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.job_id == job_id,
                )
            )
        ).scalar_one_or_none()
        if job:
            await ScheduledJobMutationFactory.apply(db, job, {"enabled": False})


async def _schedule_snapshots(db: AsyncSession, workspace: Any) -> dict[str, dict]:
    rows = list(
        (
            await db.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.entity_id == workspace.entity_id,
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.agent_initiative_id.is_(None),
                )
                .order_by(ScheduledJob.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    output = {}
    for row in rows:
        target = {
            "key": f"dependent_schedule:{row.job_id}",
            "kind": "scheduled_job",
            "identity": row.job_id,
            "row_id": row.id,
            "source_content": None,
            "derived_schedule": True,
        }
        content, row = await _read(db, workspace, target)
        output[row.id] = {
            "target": target,
            "content": content,
            "fingerprint": _current_fingerprint(content, row),
            "references": {"agent_id": row.agent_id, "execution_target": copy.deepcopy(row.execution_target)},
        }
    return output


async def _reconcile_subscription_schedules(db: AsyncSession, workspace: Any) -> None:
    """A schedule's frozen Agent link follows its stable service deployment."""
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory

    rows = list(
        (
            await db.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.entity_id == workspace.entity_id,
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.execution_type == "agent",
                    ScheduledJob.agent_initiative_id.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        service_key = (row.execution_target or {}).get("service_key")
        if not service_key:
            continue
        subscriptions = list(
            (
                await db.execute(
                    select(AgentSubscription).where(
                        AgentSubscription.entity_id == workspace.entity_id,
                        AgentSubscription.workspace_id == workspace.id,
                        AgentSubscription.service_key == service_key,
                        AgentSubscription.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(subscriptions) != 1:
            if row.enabled:
                raise ConfigurationUpgradeError("An enabled schedule requires one active subscription")
            continue
        if row.agent_id != subscriptions[0].agent_id:
            await ScheduledJobMutationFactory.apply(db, row, {"agent_id": subscriptions[0].agent_id})


async def _reconcile_component_references(db: AsyncSession, workspace: Any, remaps: dict, restore: list[dict]) -> None:
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory

    rows = list(
        (
            await db.execute(
                select(ScheduledJob)
                .where(
                    ScheduledJob.entity_id == workspace.entity_id,
                    ScheduledJob.workspace_id == workspace.id,
                    ScheduledJob.agent_initiative_id.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        patch = {}
        agent_id = remaps.get("agents", {}).get(row.agent_id, row.agent_id)
        target = _remap_runtime_references(row.execution_target, remaps)
        if row.agent_id != agent_id:
            patch["agent_id"] = agent_id
        if row.execution_target != target:
            patch["execution_target"] = target
        if patch:
            await ScheduledJobMutationFactory.apply(db, row, patch)
    before = copy.deepcopy(workspace.operating_model or {})
    after = _remap_runtime_references(before, remaps)
    for path, old, new in _changed_leaf_paths(before, after, ["operating_model"]):
        # These exact local IDs live only in the protected restore receipt;
        # portable source/installed projections never contain them.
        target = {
            "key": "runtime_reference:" + "/".join(map(str, path)),
            "kind": "workspace_configuration",
            "identity": "/".join(map(str, path)),
            "path": path,
            "source_content": None,
            "runtime_reference": True,
        }
        _write_path(workspace, path, {"value": new})
        restore.append(
            {
                "target": target,
                "before": {"value": old},
                "after": {"value": new},
                "expected_fingerprint": _current_fingerprint({"value": new}, None),
            }
        )
    if before != after:
        workspace.operation_revision = int(workspace.operation_revision or 0) + 1
        await db.flush()


async def _refresh_subscription_framings(db: AsyncSession, workspace: Any, restore: list[dict]) -> None:
    from packages.core.services.workspace_operation_service import (
        _is_auto_framing_prompt,
        refresh_workspace_subscription_framings,
    )

    rows = list(
        (
            await db.execute(
                select(AgentSubscription)
                .where(
                    AgentSubscription.entity_id == workspace.entity_id,
                    AgentSubscription.workspace_id == workspace.id,
                    AgentSubscription.status == "active",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    before = {}
    for row in rows:
        if _is_auto_framing_prompt(row.custom_prompt):
            target = {
                "key": f"subscription_framing:{row.id}",
                "kind": "subscription",
                "identity": row.service_key or row.id,
                "row_id": row.id,
                "source_content": None,
            }
            content, _ = await _read(db, workspace, target)
            before[row.id] = (target, content, row.agent_id)
    await refresh_workspace_subscription_framings(db, workspace)
    for row in rows:
        if row.id not in before:
            continue
        target, content, agent_id = before[row.id]
        actual, _ = await _read(db, workspace, target)
        if content == actual:
            continue
        existing = next(
            (
                entry
                for entry in restore
                if entry["target"]["kind"] == "subscription" and entry["target"].get("row_id") == row.id
            ),
            None,
        )
        if existing is not None:
            actual, row = await _read(db, workspace, existing["target"])
            existing["after"] = actual
            existing["expected_fingerprint"] = _current_fingerprint(actual, row)
        else:
            restore.append(
                {
                    "target": target,
                    "before": content,
                    "after": actual,
                    "references_before": {"agent_id": agent_id},
                    "expected_fingerprint": _current_fingerprint(actual, row),
                }
            )


async def _sync_heartbeat(
    db: AsyncSession, workspace: Any, *, user_id: str | None = None,
) -> None:
    from packages.core.services.scheduler_service import ScheduledJobMutationFactory
    from packages.core.strategist.evolution_scheduling import install_evolution_schedules
    from packages.core.strategist.scheduling import install_strategist_schedule

    await db.flush()
    if workspace.status == "active" and workspace.heartbeat_enabled:
        await install_strategist_schedule(
            db, workspace, cadence=workspace.heartbeat_cadence or "daily", user_id=user_id,
        )
        await install_evolution_schedules(db, workspace)
    else:
        rows = list(
            (
                await db.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.entity_id == workspace.entity_id,
                        ScheduledJob.workspace_id == workspace.id,
                        ScheduledJob.job_id.in_([f"sr:{workspace.id}", f"oe:{workspace.id}", f"cie:{workspace.id}"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await ScheduledJobMutationFactory.apply(db, row, {"enabled": False})
    for kind, model in (("goal", Goal), ("stat", WorkspaceStat)):
        rows = list(
            (
                await db.execute(
                    select(model).where(model.workspace_id == workspace.id, model.entity_id == workspace.entity_id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            await _sync_collection(db, workspace, kind, row)


async def _write(
    db: AsyncSession, workspace: Any, target: dict, content: Any, *, payload: dict, actor: Any, by_user_id: str | None
) -> None:
    kind = target["kind"]
    if kind == "workspace_configuration":
        if target["path"] == ["operating_model", "app_view"] and content is not None:
            from packages.core.blueprints.app_view import remap_workspace_app_view
            content = {"value": await remap_workspace_app_view(db, workspace, content["value"], to_runtime=True)}
        _write_path(workspace, target["path"], content)
        workspace.operation_revision = int(workspace.operation_revision or 0) + 1
        await db.flush()
        return
    if kind == "governance":
        from packages.core.governance.policy import policy_from_dict
        from packages.core.governance.service import update_policy

        row = await update_policy(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            policy=policy_from_dict(content or {}),
            changed_by=by_user_id,
            change_summary="Blueprint configuration upgrade",
        )
        if isinstance((workspace.operating_model or {}).get("governance"), dict):
            # Runtime repair can rebuild from this source mirror. Keep it
            # derived from the persisted owner-bound policy on upgrade/undo.
            operating = copy.deepcopy(workspace.operating_model)
            if operating["governance"] != row.policy:
                operating["governance"] = copy.deepcopy(row.policy)
                workspace.operating_model = operating
                workspace.operation_revision = int(workspace.operation_revision or 0) + 1
                await db.flush()
        return
    row = await _row(db, workspace, target, lock=True)
    if content is None:
        await _retire(db, workspace, target, row, actor=actor, by_user_id=by_user_id)
        return
    if kind == "subscription":
        from packages.core.blueprints.upgrade import _require_component_access
        from packages.core.services.reusable_resource_locks import lock_reusable_resource_reference

        agent = await _resolve_agent(
            db,
            workspace,
            content,
            payload,
            by_user_id=by_user_id,
            restore_agent_id=(target.get("restore_references") or {}).get("agent_id"),
        )
        from packages.core.models.permission import Capability

        await _require_component_access(
            db, row=agent, kind="agent", entity_id=workspace.entity_id, actor=actor, capability=Capability.VIEW
        )
        if "restore_references" not in target:
            from packages.core.blueprints.installer import _enforce_governance_against_agent
            from packages.core.governance.service import get_policy
            from packages.core.models.workspace import AgentToolBinding, ToolDefinition

            tools = list(
                (
                    await db.execute(
                        select(ToolDefinition.name)
                        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
                        .where(AgentToolBinding.agent_id == agent.id)
                    )
                ).scalars()
            )
            _enforce_governance_against_agent(
                {"slug": agent.slug, "tool_bindings": tools}, await get_policy(db, workspace.id)
            )
        await lock_reusable_resource_reference(
            db, entity_id=workspace.entity_id, resource_type="agent", resource_id=agent.id
        )
        if row is None:
            row = AgentSubscription(
                id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id, agent_id=agent.id
            )
            db.add(row)
        # The orchestration's channel journal reconciles and restores every
        # route after all subscription/component identities are final.
        row.agent_id = agent.id
        for key in _FIELDS[kind]:
            value = _merge_portable_dict(row.config, content[key]) if key == "config" else copy.deepcopy(content[key])
            setattr(row, key, value)
        await db.flush()
        if row.status == "active":
            from packages.core.workers.registry import ensure_internal_worker

            await ensure_internal_worker(db, workspace.entity_id)
    elif kind == "scheduled_job":
        from packages.core.constants.blueprints import installed_blueprint_job_id
        from packages.core.services.product_growth import persist_scheduled_job
        from packages.core.services.scheduler_service import ScheduledJobMutationFactory

        values = (
            await _restored_job_fields(db, workspace, content, target["restore_references"])
            if "restore_references" in target
            else await _job_fields(db, workspace, content, payload, actor=actor, by_user_id=by_user_id)
        )
        if row is None:
            row = ScheduledJob(
                id=generate_ulid(),
                job_id=installed_blueprint_job_id(target["identity"], workspace.id),
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                user_id=by_user_id,
                **values,
            )
            await persist_scheduled_job(db, row)
        else:
            await ScheduledJobMutationFactory.apply(db, row, values, changed_by_kind="user", changed_by_id=by_user_id)
    elif kind == "stat":
        from packages.core.stats.service import _validate_definition, create_stat
        from packages.core.stats.assessment import validate_review_definition

        validate_review_definition(content["collector_type"], content["collector_config"], content["collection_cadence"])

        _validate_definition(
            key=content["key"],
            value_type=content["value_type"],
            collector_type=content["collector_type"],
            window=content["window"],
        )
        if row is None:
            row = await create_stat(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                origin="blueprint",
                install_schedule=False,
                **{key: value for key, value in content.items() if key != "status"},
            )
        else:
            changed = False
            for key in _FIELDS[kind]:
                value = (
                    _merge_portable_dict(row.collector_config, content[key])
                    if key == "collector_config"
                    else copy.deepcopy(content[key])
                )
                if _json(getattr(row, key)) != value:
                    setattr(row, key, value)
                    changed = True
            if changed:
                row.revision += 1
        row.status = content["status"]
        await _sync_collection(db, workspace, kind, row)
    elif kind == "goal":
        from packages.core.goals import create_goal
        from packages.core.goals.commands import GoalUpdateCommand
        from packages.core.goals.numbers import validate_goal_number
        from packages.core.goals.service import _sync_workspace_goal_contract
        from packages.core.goals.scheduling import validate_measurement_cadence
        from packages.core.revisions import bump_revision

        values = {key: value for key, value in content.items() if key not in {"goal_key", "stat_key", "retired"}}
        values["deadline"] = date.fromisoformat(values["deadline"]) if values.get("deadline") else None
        for key in ("target_value", "baseline_value"):
            if values.get(key) is not None:
                values[key] = validate_goal_number(values[key])
        if values.get("measurement_cadence"):
            values["measurement_cadence"] = validate_measurement_cadence(values["measurement_cadence"])
        stat = (
            (
                await db.execute(
                    select(WorkspaceStat).where(
                        WorkspaceStat.entity_id == workspace.entity_id,
                        WorkspaceStat.workspace_id == workspace.id,
                        WorkspaceStat.key == content["stat_key"],
                        WorkspaceStat.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if content.get("stat_key")
            else None
        )
        if content.get("stat_key") and stat is None:
            raise ConfigurationUpgradeError("Goal requires an active Stat in this Workspace")
        values["stat_id"] = stat.id if stat else None
        if row is not None and isinstance(values.get("measurement_source"), dict):
            values["measurement_source"] = _merge_portable_dict(row.measurement_source, values["measurement_source"])
        GoalUpdateCommand.from_fields(values)
        if row is None:
            row = await create_goal(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                goal_key=content["goal_key"],
                install_schedule=False,
                **values,
            )
        else:
            patch = {key: value for key, value in values.items() if getattr(row, key) != value}
            if "stat_id" in patch:
                # A new Stat has a different observation contract. Keep every
                # measurement row, but never display the old Stat's cached
                # value as if it measured the new definition.
                row.current_value = None
                row.current_value_updated_at = None
                row.pace_status = None
                row.pace_computed_at = None
                row.achieved_at = None
            for key, value in patch.items():
                setattr(row, key, copy.deepcopy(value))
            if row.status not in {"achieved", "abandoned"}:
                status = "paused" if content.get("retired") else "active"
                if row.status != status:
                    row.status = status
                    patch["status"] = status
            if patch:
                await bump_revision(db, row, patch=patch, changed_by_kind="user", changed_by_id=by_user_id)
            await db.flush()
            await _sync_workspace_goal_contract(db, row)
        await _sync_collection(db, workspace, kind, row)
    elif kind == "custom_field":
        if row is None:
            row = CustomFieldDefinition(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id)
            db.add(row)
        for key in _FIELDS[kind]:
            setattr(row, key, copy.deepcopy(content[key]))
    elif kind == "knowledge_pack":
        if row is None:
            row = DocumentGroup(id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id)
            db.add(row)
        row.workspace_id = workspace.id
        row.name = content["title"]
        row.settings = {
            **dict(row.settings or {}),
            **{key: copy.deepcopy(content[key]) for key in ("purpose", "folder_structure", "mode", "external_source")},
            "installed_from_blueprint_slug": target["identity"],
            "blueprint_retired": bool(content.get("retired")),
        }
        row.settings.pop("blueprint_detached_workspace_id", None)
    elif kind == "knowledge_document":
        from packages.core.blueprints.installer import (
            _materialize_knowledge_pack_document,
            _require_blueprint_document_owned_by_workspace,
        )
        from packages.core.blueprints.upgrade import _require_document_access
        from packages.core.models.permission import Capability
        from packages.core.services.document_service import add_document_to_group

        pack = await _row(
            db, workspace, {"kind": "knowledge_pack", "identity": target["pack_slug"], "source_content": {}}
        )
        if pack is None:
            raise ConfigurationUpgradeError("Starter document requires its Knowledge pack")
        if row is None:
            row, _ = await _materialize_knowledge_pack_document(
                db,
                entity_id=workspace.entity_id,
                workspace_id=workspace.id,
                group_id=pack.id,
                knowledge_pack_slug=target["pack_slug"],
                document=content,
            )
            if row is None:
                raise ConfigurationUpgradeError("Starter document has no materializable content")
            row.owner_id = by_user_id
            row.created_by = by_user_id
        else:
            if _document_content(row) == content:
                # Undoing a detach changes only this Workspace's membership;
                # stored files and shared documents need no body overwrite.
                await _require_document_access(db, row=row, actor=actor, capability=Capability.MANAGE_METADATA)
                await add_document_to_group(db, row.id, pack.id, entity_id=workspace.entity_id)
                await db.flush()
                target["row_id"] = row.id
                return
            await _require_blueprint_document_owned_by_workspace(db, row, workspace.id)
            await _require_document_access(db, row=row, actor=actor, capability=Capability.EDIT)
            if row.fs_path or row.file_url:
                raise ConfigurationUpgradeError(
                    "Stored-file starter content must be reconciled through the document editor"
                )
            from packages.core.services.document_service import _require_text_content_save

            _require_text_content_save(row)
            await add_document_to_group(db, row.id, pack.id, entity_id=workspace.entity_id)
            metadata = dict(row.metadata_ or {})
            body_changed = metadata.get("content", metadata.get("content_text", "")) != content["body_md"]
            metadata.update(
                {
                    "content_text": content["body_md"],
                    "blueprint_starter_path": content["path"],
                    "blueprint_knowledge_pack_slug": target["pack_slug"],
                }
            )
            if "content" in metadata:
                metadata["content"] = content["body_md"]
            for key, value in (
                ("blueprint_document_key", content.get("key")),
                ("blueprint_template", content.get("template")),
            ):
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = copy.deepcopy(value)
            row.metadata_ = metadata
            row.name = content["path"]
            row.file_size = len(content["body_md"].encode("utf-8"))
            if body_changed:
                row.vector_status = "pending"
                await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == row.id))
                from packages.core.services.version_service import create_version

                await create_version(
                    db,
                    row.id,
                    workspace.entity_id,
                    change_summary="Blueprint configuration upgrade",
                    created_by=by_user_id,
                )
    await db.flush()
    if row is not None:
        target["row_id"] = row.id


async def _retire(
    db: AsyncSession, workspace: Any, target: dict, row: Any, *, actor: Any, by_user_id: str | None
) -> None:
    if row is None:
        return
    kind = target["kind"]
    if kind == "scheduled_job":
        from packages.core.services.scheduler_service import ScheduledJobMutationFactory

        await ScheduledJobMutationFactory.apply(
            db, row, {"enabled": False}, changed_by_kind="user", changed_by_id=by_user_id
        )
    elif kind in {"subscription", "custom_field", "stat"}:
        if kind == "stat":
            linked = (
                await db.execute(
                    select(Goal.id)
                    .where(
                        Goal.workspace_id == workspace.id,
                        Goal.entity_id == workspace.entity_id,
                        Goal.stat_id == row.id,
                        Goal.status == "active",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if linked:
                raise ConfigurationUpgradeError("An active Goal still uses the Stat selected for removal")
        if kind == "subscription":
            linked = (
                await db.execute(
                    select(ScheduledJob.id)
                    .where(
                        ScheduledJob.workspace_id == workspace.id,
                        ScheduledJob.entity_id == workspace.entity_id,
                        ScheduledJob.enabled.is_(True),
                        ScheduledJob.execution_target["service_key"].astext == row.service_key,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if linked:
                raise ConfigurationUpgradeError("An enabled schedule still uses the subscription selected for removal")
        row.status = "inactive"
        if kind == "stat":
            row.revision += 1
            await _sync_collection(db, workspace, kind, row)
    elif kind == "goal":
        if row.status == "active":
            from packages.core.revisions import bump_revision

            row.status = "paused"
            await bump_revision(db, row, patch={"status": "paused"}, changed_by_kind="user", changed_by_id=by_user_id)
        await _sync_collection(db, workspace, kind, row)
    elif kind == "knowledge_pack":
        row.settings = {
            **dict(row.settings or {}),
            "blueprint_retired": True,
            "blueprint_detached_workspace_id": workspace.id,
        }
        row.workspace_id = None
    elif kind == "knowledge_document":
        from packages.core.blueprints.upgrade import _require_document_access
        from packages.core.models.permission import Capability

        await _require_document_access(db, row=row, actor=actor, capability=Capability.MANAGE_METADATA)
        pack = await _row(
            db, workspace, {"kind": "knowledge_pack", "identity": target["pack_slug"], "source_content": {}}
        )
        if pack:
            await db.execute(
                delete(DocumentGroupMember).where(
                    DocumentGroupMember.document_id == row.id, DocumentGroupMember.group_id == pack.id
                )
            )
    await db.flush()


async def apply_configuration(
    db: AsyncSession,
    *,
    workspace: Any,
    payload: dict,
    plan: dict,
    choices: dict[str, str] | None = None,
    by_user_id: str | None = None,
    actor: Any = None,
    reference_remaps: dict[str, dict[str, str]] | None = None,
) -> dict:
    workspace = await _lock_workspace(db, workspace)
    choices = choices or {}
    targets = copy.deepcopy(plan["targets"])
    items = {item["key"]: item for item in plan["items"]}
    if set(choices) - set(items):
        raise ConfigurationUpgradeError("Configuration choices no longer match the reviewed plan")
    # Validate every row before the first write. Runtime counters are excluded
    # from this CAS; an unrelated observation may arrive without invalidating it.
    for key, target in targets.items():
        current, row = await _read(db, workspace, target, lock=True)
        if _current_fingerprint(current, row) != items[key]["expected_fingerprint"]:
            raise ConfigurationUpgradeError("Workspace configuration changed after review")
        decision = choices.get(key)
        if decision not in {None, "keep_yours", "use_blueprint"}:
            raise ConfigurationUpgradeError("Invalid configuration choice")
        if items[key]["action"] == "keep_yours" and decision is None:
            raise ConfigurationUpgradeError("Review every configuration conflict before upgrading")
        if items[key].get("blocked_reason") and decision != "keep_yours":
            raise ConfigurationUpgradeError(items[key]["blocked_reason"])
    schedules_before = await _schedule_snapshots(db, workspace)
    selected_governance = _selected_governance_content(workspace, plan, choices)
    restore, updated, kept = [], [], []
    ordered = sorted(
        targets.values(),
        key=lambda value: (
            value.get("path") == ["operating_model", "app_view"],
            value["source_content"] is None,
            -_ORDER[value["kind"]] if value["source_content"] is None else _ORDER[value["kind"]],
            value["key"],
        ),
    )
    for target in ordered:
        item = items[target["key"]]
        if target["kind"] == "governance":
            # Preserve the author's desired target in the next baseline. The
            # chosen source projection is only the actual write/undo receipt.
            target = {**target, "source_content": selected_governance}
            skip = item["current_content"] == selected_governance
            if choices.get(target["key"]) == "keep_yours" and item["new_content"] != selected_governance:
                kept.append(item)
        else:
            skip = choices.get(target["key"]) == "keep_yours" or item["action"] == "unchanged"
        if skip:
            if item["current_content"] != target["source_content"]:
                kept.append(item)
            continue
        before, row = await _read(db, workspace, target, lock=True)
        references_before = (
            {"agent_id": row.agent_id, "execution_target": copy.deepcopy(row.execution_target)}
            if row is not None and target["kind"] == "scheduled_job"
            else None
        )
        if row is not None and target["kind"] == "subscription":
            references_before = {"agent_id": row.agent_id}
        await _write(
            db, workspace, target, target["source_content"], payload=payload, actor=actor, by_user_id=by_user_id
        )
        after, row = await _read(db, workspace, target, lock=True)
        if target["source_content"] is not None and after != target["source_content"]:
            raise ConfigurationUpgradeError(f"Configuration verification failed for {target['key']}")
        if target["source_content"] is None and not _is_retired(target["kind"], after):
            if target["kind"] != "goal" or row.status not in {"achieved", "abandoned"}:
                raise ConfigurationUpgradeError(f"Configuration retirement failed for {target['key']}")
        restore.append(
            {
                "target": copy.deepcopy(target),
                "before": before,
                "after": after,
                "references_before": references_before,
                "expected_fingerprint": _current_fingerprint(after, row),
            }
        )
        updated.append({"kind": target["kind"], "key": target["key"], "name": item["name"], "changes": item["changes"]})
    if any(entry["target"]["kind"] == "subscription" for entry in restore):
        await _reconcile_subscription_schedules(db, workspace)
    if reference_remaps:
        await _reconcile_component_references(db, workspace, reference_remaps, restore)
    if any(entry["target"].get("path", [None])[0] in {"operating_context", "primary_work"} for entry in restore):
        await _refresh_subscription_framings(db, workspace, restore)
    if any(entry["target"].get("path", [None])[0] in {"heartbeat_enabled", "heartbeat_cadence"} for entry in restore):
        await _sync_heartbeat(db, workspace, user_id=by_user_id)
    schedules_after = await _schedule_snapshots(db, workspace)
    for row_id, state in schedules_after.items():
        existing = next(
            (
                entry
                for entry in restore
                if entry["target"]["kind"] == "scheduled_job" and entry["target"].get("row_id") == row_id
            ),
            None,
        )
        if existing is not None:
            existing["after"] = state["content"]
            existing["expected_fingerprint"] = state["fingerprint"]
            continue
        before = schedules_before.get(row_id)
        if (
            before is not None
            and before["fingerprint"] == state["fingerprint"]
            and before["references"] == state["references"]
        ):
            continue
        restore.append(
            {
                "target": state["target"],
                "before": before["content"] if before else None,
                "after": state["content"],
                "references_before": before["references"] if before else None,
                "expected_fingerprint": state["fingerprint"],
            }
        )
    for target in targets.values():
        target["installed_content"], _ = await _read(db, workspace, target)
    return {"restore": restore, "baseline": {"version": 1, "entries": targets}, "updated": updated, "kept": kept}


async def validate_configuration_restore(
    db: AsyncSession, *, workspace: Any, restore: list[dict], actor: Any = None
) -> None:
    workspace = await _lock_workspace(db, workspace)
    for entry in restore:
        current, row = await _read(db, workspace, copy.deepcopy(entry["target"]), lock=True)
        if _current_fingerprint(current, row) != entry["expected_fingerprint"]:
            raise ConfigurationUpgradeError("An upgraded configuration changed; review it before undoing")
        kind = entry["target"]["kind"]
        if kind == "subscription" and row is not None:
            references = entry.get("references_before") or {}
            if references.get("agent_id"):
                from packages.core.blueprints.upgrade import _require_component_access
                from packages.core.models.permission import Capability

                agent = await _resolve_agent(
                    db, workspace, entry["before"], {}, restore_agent_id=references["agent_id"]
                )
                await _require_component_access(
                    db, row=agent, kind="agent", entity_id=workspace.entity_id, actor=actor, capability=Capability.VIEW
                )
        if kind == "scheduled_job" and entry.get("references_before") is not None and entry["before"] is not None:
            await _restored_job_fields(db, workspace, entry["before"], entry["references_before"])
        if row is not None and entry["target"]["kind"] == "knowledge_document":
            from packages.core.blueprints.upgrade import _require_document_access
            from packages.core.models.permission import Capability

            await _require_document_access(
                db,
                row=row,
                actor=actor,
                capability=Capability.MANAGE_METADATA
                if entry["before"] is None or _document_content(row) == entry["before"]
                else Capability.EDIT,
            )
            if entry["before"] is not None and _document_content(row) != entry["before"]:
                from packages.core.blueprints.installer import _require_blueprint_document_owned_by_workspace
                from packages.core.services.document_service import _require_text_content_save

                await _require_blueprint_document_owned_by_workspace(db, row, workspace.id)
                _require_text_content_save(row)
                if row.fs_path or row.file_url:
                    raise ConfigurationUpgradeError(
                        "Stored-file starter content must be reconciled through the document editor"
                    )


async def revert_configuration(
    db: AsyncSession,
    *,
    workspace: Any,
    restore: list[dict],
    actor: Any = None,
    by_user_id: str | None = None,
    payload: dict | None = None,
) -> dict:
    await validate_configuration_restore(db, workspace=workspace, restore=restore, actor=actor)
    workspace = await _lock_workspace(db, workspace)
    reverted = []
    # Dependency identities remain in place. Undoing an added object retires it
    # instead of deleting rows that may already have observations or run history.
    ordered_restore = [entry for entry in reversed(restore) if not entry["target"].get("derived_schedule")]
    ordered_restore += [entry for entry in reversed(restore) if entry["target"].get("derived_schedule")]
    for entry in ordered_restore:
        target = copy.deepcopy(entry["target"])
        if entry.get("references_before") is not None:
            target["restore_references"] = entry["references_before"]
        await _write(db, workspace, target, entry["before"], payload=payload or {}, actor=actor, by_user_id=by_user_id)
        actual, row = await _read(db, workspace, target, lock=True)
        if entry["before"] is not None and actual != entry["before"]:
            raise ConfigurationUpgradeError("Configuration restore verification failed")
        reverted.append({"kind": target["kind"], "name": target["identity"]})
    return {"reverted": reverted}
