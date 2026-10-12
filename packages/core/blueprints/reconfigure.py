"""Reviewed, transactional in-place Blueprint upgrades (protocol 3).

Portable definitions are reconciled against a durable source/installed baseline.
Workspace identity and runtime records are never replaced or restored wholesale.
The caller owns commit; a savepoint also makes failures atomic for direct callers.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from sqlalchemy import or_, select

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY, BLUEPRINT_SETTINGS_KEY, BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY, LAST_PARTIAL_UPGRADE_FINGERPRINT_KEY,
    LAST_PARTIAL_UPGRADE_VERSION_KEY, MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY, UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint, blueprint_section_fingerprints,
    blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.blueprints.upgrade import (
    BlueprintUpgradePlanChangedError, LIVE_SETUP_STATE_KEYS,
)
from packages.core.blueprints.upgrade_activity import active_runs
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState

PROTOCOL_VERSION = 3
RUNTIME_KEY = "runtime_requirements"
RECORD_KEYS = (
    BLUEPRINT_ID_KEY, BLUEPRINT_VERSION_KEY, CONTENT_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY, UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    LAST_PARTIAL_UPGRADE_FINGERPRINT_KEY, LAST_PARTIAL_UPGRADE_VERSION_KEY,
    "variable_declarations", "channel_requirements", "session_requirements",
    "synchronization_status", "upgrade_runtime_contract", "upgrade_protocol_version",
    "restore_point", *LIVE_SETUP_STATE_KEYS,
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def used_variable_payload(payload: dict) -> dict:
    """Remove declarations that cannot affect installation, including dead forms.

    Materialized personalization is itself a supported consumer. Other variables
    need an actual {{key}} occurrence outside the declaration list.
    """
    result = deepcopy(payload)
    contract = result.setdefault("contract", {})
    declarations = contract.pop("variables", [])
    used = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", json.dumps(result)))
    contract["variables"] = [
        item for item in declarations
        if item.get("materialize") or item.get("key") in used
    ]
    return result


def runtime_contract(payload: dict) -> dict:
    return {
        "contract": {key: deepcopy(value) for key, value in (payload.get("contract") or {}).items()
                     if key != "variables"},
        "post_install_checks": deepcopy((payload.get("policy") or {}).get("post_install_checks", [])),
        "expected_baseline": deepcopy((payload.get("policy") or {}).get("expected_baseline")),
    }


async def _state(db, workspace, *, lock=False):
    statement = select(WorkspaceBlueprintUpgradeState).where(
        WorkspaceBlueprintUpgradeState.workspace_id == workspace.id,
        WorkspaceBlueprintUpgradeState.entity_id == workspace.entity_id,
    ).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def capture_install_baseline(db, *, workspace, payload):
    from packages.core.blueprints.component_upgrade import capture_components
    from packages.core.blueprints.configuration_upgrade import capture_configuration

    await db.flush()
    baseline = {
        "components": await capture_components(db, workspace=workspace, payload=payload),
        "configuration": await capture_configuration(db, workspace=workspace, payload=payload),
        "runtime": {"source": runtime_contract(payload), "installed": runtime_contract(payload)},
    }
    state = await _state(db, workspace, lock=True)
    if state is None:
        db.add(WorkspaceBlueprintUpgradeState(
            workspace_id=workspace.id, entity_id=workspace.entity_id, baseline=baseline,
        ))
    else:
        state.baseline = baseline
        state.restore_point = None
        state.revision += 1
    await db.flush()


def _runtime_item(workspace, payload, baseline):
    record = (workspace.settings or {}).get(BLUEPRINT_SETTINGS_KEY) or {}
    previous = (baseline or {}).get("runtime")
    current = deepcopy(record.get("upgrade_runtime_contract"))
    if current is None:
        current = deepcopy(previous.get("installed")) if previous else {
            "channels": record.get("channel_requirements", []),
            "sessions": record.get("session_requirements", []),
            "live_setup_requirements": record.get("live_setup_requirements", []),
        }
    desired = runtime_contract(payload)
    action = "unchanged" if current == desired or (previous and previous.get("source") == desired) else (
        "update" if previous and current == previous.get("installed") else "keep_yours"
    )
    return {
        "key": RUNTIME_KEY, "kind": "runtime_requirements", "slug": RUNTIME_KEY,
        "name": "Runtime requirements, channels and sessions", "action": action,
        "current_content": current, "new_content": desired,
        "baseline_content": previous.get("source") if previous else None,
        "baseline_known": bool(previous), "changes": ["runtime setup contract"],
    }


async def plan(db, *, workspace, payload, source_payload=None, current_version=None,
               source_blueprint_id=None, channel_config_ids=None, actor=None):
    from packages.core.blueprints.component_upgrade import plan_components
    from packages.core.blueprints.configuration_upgrade import plan_configuration

    state = await _state(db, workspace)
    baseline = state.baseline if state else None
    record = (workspace.settings or {}).get(BLUEPRINT_SETTINGS_KEY) or {}
    component_plan = await plan_components(db, workspace=workspace, payload=payload,
                                           baseline=(baseline or {}).get("components"), actor=actor)
    configuration_plan = await plan_configuration(db, workspace=workspace, payload=payload,
                                                  baseline=(baseline or {}).get("configuration"), actor=actor)
    items = [*component_plan["items"], *configuration_plan["items"],
             _runtime_item(workspace, payload, baseline)]
    upstream = blueprint_content_fingerprint(source_payload or payload)
    material = {
        "workspace_id": workspace.id, "revision": state.revision if state else None,
        "items": items, "upstream": upstream, "version": current_version,
        "channels": channel_config_ids or {}, "record": _metadata(workspace),
        "routing": await _routing_state(db, workspace=workspace),
    }
    point = (state.restore_point if state else None) or record.get("restore_point")
    return {
        "upgrade_protocol_version": PROTOCOL_VERSION,
        "blueprint_fingerprint": upstream, "plan_fingerprint": fingerprint(material),
        "blueprint_name": (source_payload or payload).get("manifest", {}).get("title"),
        "installed_version": record.get(BLUEPRINT_VERSION_KEY), "current_version": current_version,
        "synchronization_status": record.get("synchronization_status"),
        "blueprint_id": source_blueprint_id or record.get(BLUEPRINT_ID_KEY),
        "items": items, "active_runs": await active_runs(db, workspace=workspace),
        "baseline_known": bool(baseline), "can_revert": bool(point),
        "restore_point": {**{key: point.get(key) for key in ("from_version", "to_version")},
                          "applied_at": point.get("applied_at") or point.get("upgraded_at"),
                          "blueprint_version": point.get("to_version")}
        if point else None,
        "unsupported_changes": [],
        "_components": component_plan, "_configuration": configuration_plan,
    }


def public_plan(value):
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _metadata(workspace):
    settings = workspace.settings or {}
    record = settings.get(BLUEPRINT_SETTINGS_KEY) or {}
    return {
        "record": {key: deepcopy(record[key]) for key in RECORD_KEYS if key in record},
        "settings": {key: deepcopy(settings[key]) for key in ("blueprint_personalization",)
                     if key in settings},
    }


def _restore_metadata(workspace, before):
    settings = deepcopy(workspace.settings or {})
    record = settings.setdefault(BLUEPRINT_SETTINGS_KEY, {})
    for key in RECORD_KEYS:
        record.pop(key, None)
    record.update(deepcopy(before["record"]))
    settings.pop("blueprint_personalization", None)
    settings.update(deepcopy(before["settings"]))
    workspace.settings = settings


def _choices(items, resolutions):
    result = {}
    known = {item["key"]: item for item in items}
    for resolution in resolutions:
        key, choice = resolution.get("key"), resolution.get("resolution")
        if key not in known or key in result or choice not in ("keep_yours", "use_blueprint"):
            raise BlueprintUpgradePlanChangedError("Invalid or duplicate configuration choice. Refresh the preview.")
        result[key] = choice
    if any(item["action"] != "unchanged" and item["key"] not in result for item in items):
        raise BlueprintUpgradePlanChangedError("Review every configuration difference before applying.")
    return result


async def _lock(db, workspace):
    from packages.core.services.reusable_resource_locks import lock_reusable_resource_lifecycle
    from packages.core.services.workspace_access import lock_workspace_access_boundary

    await lock_reusable_resource_lifecycle(db, entity_id=workspace.entity_id)
    row = await lock_workspace_access_boundary(db, workspace_id=workspace.id, entity_id=workspace.entity_id)
    if row is None or row.deleted_at is not None:
        raise BlueprintUpgradePlanChangedError("Workspace is no longer available.")
    if await active_runs(db, workspace=row):
        raise BlueprintUpgradePlanChangedError("Workspace has active executions. Wait for them to finish and refresh the preview.")
    return row


async def apply(db, *, workspace, payload, source_payload, by_user_id, actor,
                current_version, source_blueprint_id, expected_blueprint_fingerprint,
                expected_plan_fingerprint, configuration_resolutions,
                personalization=None, channel_config_ids=None, resolved_channel_config_ids=None):
    from packages.core.blueprints.component_upgrade import apply_components
    from packages.core.blueprints.configuration_upgrade import (
        apply_configuration, effective_configuration_policy,
    )
    from packages.core.blueprints.installer import (
        _assert_runtime_compatible, _bind_blueprint_channel_configs,
        sync_workspace_live_setup_requirements,
    )
    from packages.core.blueprints.payload import validate_payload

    validate_payload(payload)
    _assert_runtime_compatible(payload)
    async with db.begin_nested():
        workspace = await _lock(db, workspace)
        intended = await plan(
            db, workspace=workspace, payload=payload, source_payload=source_payload,
            current_version=current_version, source_blueprint_id=source_blueprint_id,
            channel_config_ids=channel_config_ids, actor=actor,
        )
        if (intended["blueprint_fingerprint"] != expected_blueprint_fingerprint
                or intended["plan_fingerprint"] != expected_plan_fingerprint):
            raise BlueprintUpgradePlanChangedError("Workspace or Blueprint changed. Refresh and review the new preview.")
        choices = _choices(intended["items"], configuration_resolutions)
        component_keys = {item["key"] for item in intended["_components"]["items"]}
        configuration_keys = {item["key"] for item in intended["_configuration"]["items"]}
        component_choices = {key: value for key, value in choices.items() if key in component_keys}
        configuration_choices = {key: value for key, value in choices.items() if key in configuration_keys}
        state = await _state(db, workspace, lock=True)
        baseline_before = deepcopy(state.baseline) if state else None
        metadata_before = _metadata(workspace)
        policy = await effective_configuration_policy(
            db, workspace=workspace, payload=payload, plan=intended["_configuration"], choices=configuration_choices,
        )
        components = await apply_components(
            db, workspace=workspace, payload=payload, plan=intended["_components"], choices=component_choices,
            by_user_id=by_user_id, actor=actor, effective_policy=policy,
        )
        configuration = await apply_configuration(
            db, workspace=workspace, payload=payload, plan=intended["_configuration"], choices=configuration_choices,
            by_user_id=by_user_id, actor=actor,
            reference_remaps={kind + "s": {
                previous["id"]: components["component_ids"][key]
                for key, previous in intended["_components"].get("current", {}).items()
                if previous.get("kind") == kind and previous.get("id")
                and components["component_ids"].get(key)
                and previous["id"] != components["component_ids"][key]
            } for kind in ("agent", "skill", "workflow")},
        )
        runtime = next(item for item in intended["items"] if item["key"] == RUNTIME_KEY)
        use_runtime = runtime["current_content"] == runtime["new_content"] or (
            runtime["action"] != "unchanged" and choices.get(RUNTIME_KEY) == "use_blueprint"
        )
        channels = await _remap_channel_agents(
            db, workspace=workspace, replacements=components.get("agent_replacements", {}),
        )
        if use_runtime:
            changes = await _bind_blueprint_channel_configs(
                db, workspace=workspace,
                channel_requirements=list((payload.get("contract") or {}).get("channels") or []),
                selected_channel_config_ids=resolved_channel_config_ids or channel_config_ids or {},
                user_id=by_user_id,
            )
            # A route can be retargeted by isolation and explicit setup in the
            # same transaction. Keep the original before and final after only.
            combined = {entry["id"]: entry for entry in channels}
            for entry in changes:
                if entry["id"] in combined:
                    combined[entry["id"]]["after"] = entry["after"]
                else:
                    combined[entry["id"]] = entry
            channels = list(combined.values())
            await sync_workspace_live_setup_requirements(db, workspace=workspace, payload=payload)
        kept = list(components["kept"]) + list(configuration["kept"])
        if not use_runtime and runtime["current_content"] != runtime["new_content"]:
            kept.append(RUNTIME_KEY)
        # The component/configuration writers verify the persisted projections.
        # Acknowledged customizations are represented explicitly, not as a claim
        # that every local byte matches upstream.
        customized = bool(kept) or any(
            item["action"] == "unchanged" and item.get("current_content") != item.get("new_content")
            for item in intended["items"]
        )
        settings = deepcopy(workspace.settings or {})
        record = settings.setdefault(BLUEPRINT_SETTINGS_KEY, {})
        record.update({
            BLUEPRINT_ID_KEY: source_blueprint_id or record.get(BLUEPRINT_ID_KEY),
            BLUEPRINT_VERSION_KEY: current_version or record.get(BLUEPRINT_VERSION_KEY),
            CONTENT_FINGERPRINT_KEY: intended["blueprint_fingerprint"],
            SECTION_FINGERPRINTS_KEY: blueprint_section_fingerprints(source_payload),
            UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: blueprint_upgrade_unsupported_fingerprint(source_payload),
            MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY: blueprint_upgrade_unsupported_fingerprint(payload),
            "variable_declarations": deepcopy((source_payload.get("contract") or {}).get("variables", [])),
            "synchronization_status": "customized" if customized else "current",
            "upgrade_protocol_version": PROTOCOL_VERSION,
        })
        for key in (LAST_PARTIAL_UPGRADE_FINGERPRINT_KEY, LAST_PARTIAL_UPGRADE_VERSION_KEY, "restore_point"):
            record.pop(key, None)
        if use_runtime:
            record["upgrade_runtime_contract"] = runtime_contract(payload)
            record["channel_requirements"] = deepcopy((payload.get("contract") or {}).get("channels", []))
            record["session_requirements"] = deepcopy((payload.get("contract") or {}).get("sessions", []))
        if personalization is not None:
            settings["blueprint_personalization"] = deepcopy(personalization)
        workspace.settings = settings
        await db.flush()
        baseline = {
            "components": components["baseline"], "configuration": configuration["baseline"],
            "runtime": {"source": runtime["new_content"], "installed":
                        runtime["new_content"] if use_runtime else runtime["current_content"]},
        }
        if state is None:
            state = WorkspaceBlueprintUpgradeState(workspace_id=workspace.id, entity_id=workspace.entity_id,
                                                    baseline=baseline, revision=1)
            db.add(state)
        else:
            state.baseline = baseline
            state.revision += 1
        state.restore_point = {
            "protocol_version": PROTOCOL_VERSION, "applied_at": datetime.now(timezone.utc).isoformat(),
            "from_version": intended["installed_version"], "to_version": current_version,
            "components": components["restore"], "configuration": configuration["restore"],
            "channels": channels, "baseline_before": baseline_before,
            "metadata_before": metadata_before, "metadata_after": _metadata(workspace),
        }
        await db.flush()
        return {
            "upgrade_protocol_version": PROTOCOL_VERSION,
            "updated": components["updated"] + configuration["updated"] + [
                {"kind": "channel", "name": item.get("name")} for item in channels
            ],
            "kept_yours": kept, "can_revert": True,
            "fully_synchronized": not customized,
            "synchronization_status": "customized" if customized else "current",
        }


async def _remap_channel_agents(db, *, workspace, replacements):
    """Reconcile changed subscription/Agent references, never conversations."""
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.channel_bindings import snapshot_channel_binding, preserve_channel_binding_route_order

    candidates = (await db.execute(select(Channel).where(
        Channel.entity_id == workspace.entity_id, Channel.workspace_id == workspace.id,
        or_(Channel.agent_id.in_(sorted(replacements)), Channel.agent_subscription_id.is_not(None)),
        Channel.status == "active",
    ).order_by(Channel.id))).scalars().all()
    subscriptions = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.entity_id == workspace.entity_id, AgentSubscription.workspace_id == workspace.id,
    ))).scalars().all()
    subscriptions_by_id = {row.id: row for row in subscriptions}
    candidates = [row for row in candidates if (
        row.agent_id in replacements or (
            row.agent_subscription_id in subscriptions_by_id
            and (row.agent_id != subscriptions_by_id[row.agent_subscription_id].agent_id
                 or subscriptions_by_id[row.agent_subscription_id].status != "active")
        )
    )]
    account_ids = sorted({(row.config or {}).get("channel_config_id") for row in candidates
                          if (row.config or {}).get("channel_config_id")})
    accounts = (await db.execute(select(ChannelConfig).where(
        ChannelConfig.id.in_(account_ids), ChannelConfig.entity_id == workspace.entity_id,
    ).order_by(ChannelConfig.id).with_for_update().execution_options(populate_existing=True))).scalars().all() if account_ids else []
    by_id = {account.id: account for account in accounts}
    changes = []
    for candidate in candidates:
        row = await db.get(Channel, candidate.id, populate_existing=True, with_for_update=True)
        account = by_id.get((row.config or {}).get("channel_config_id"))
        if (account is None or account.status != "active" or account.owner_user_id != row.user_id
                or account.channel_type != row.type or account.workspace_id not in (None, workspace.id)):
            raise BlueprintUpgradePlanChangedError("An existing channel account needs repair before its Agent can be upgraded.")
        before = snapshot_channel_binding(row)
        new_agent_id = replacements.get(row.agent_id, row.agent_id)
        if row.agent_subscription_id:
            subscription = subscriptions_by_id.get(row.agent_subscription_id)
            if (subscription is None or subscription.workspace_id != workspace.id
                    or subscription.entity_id != workspace.entity_id):
                raise BlueprintUpgradePlanChangedError("Channel subscription changed during Agent upgrade.")
            new_agent_id = subscription.agent_id
        if row.type == "twilio_voice":
            from packages.core.services.voice.call_sessions import cancel_unconnected_call_sessions_for_binding
            await cancel_unconnected_call_sessions_for_binding(
                db, channel_config_id=account.id, channel_binding_id=row.id,
                reason="Blueprint Agent was upgraded before the call connected.",
            )
        row.agent_id = new_agent_id
        if row.agent_subscription_id and subscriptions_by_id[row.agent_subscription_id].status != "active":
            row.status = "inactive"
        preserve_channel_binding_route_order(row, before["updated_at"])
        await db.flush()
        changes.append({"id": row.id, "kind": "channel", "name": row.name,
                        "before": before, "after": snapshot_channel_binding(row)})
    return changes


async def _routing_state(db, *, workspace):
    from packages.core.models.document import Channel
    from packages.core.services.channel_bindings import snapshot_channel_binding

    rows = (await db.execute(select(Channel).where(
        Channel.entity_id == workspace.entity_id, Channel.workspace_id == workspace.id,
    ).order_by(Channel.id))).scalars().all()
    return [{"id": row.id, **snapshot_channel_binding(row)} for row in rows]


async def _channel_restore_rows(db, *, workspace, entries):
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel
    from packages.core.services.channel_bindings import snapshot_channel_binding

    prepared = []
    account_ids = sorted({entry.get("after", {}).get("config", {}).get("channel_config_id")
                          for entry in entries} - {None})
    accounts = (await db.execute(select(ChannelConfig).where(
        ChannelConfig.id.in_(account_ids), ChannelConfig.entity_id == workspace.entity_id,
    ).order_by(ChannelConfig.id).with_for_update().execution_options(populate_existing=True))).scalars().all() if account_ids else []
    by_id = {account.id: account for account in accounts}
    for entry in sorted(entries, key=lambda item: str(item.get("id"))):
        after = entry.get("after") or {}
        account = by_id.get((after.get("config") or {}).get("channel_config_id"))
        row = await db.get(Channel, entry.get("id"), populate_existing=True, with_for_update=True)
        if (row is None or row.entity_id != workspace.entity_id or row.workspace_id != workspace.id
                or snapshot_channel_binding(row) != after or account is None
                or account.entity_id != workspace.entity_id or account.status != "active"
                or account.channel_type != row.type or account.owner_user_id != after.get("user_id")
                or account.workspace_id not in (None, workspace.id)):
            raise BlueprintUpgradePlanChangedError("A channel binding or its account changed after the upgrade.")
        before = entry.get("before")
        if before is not None and (
            set(before) != set(after) or any(before[key] != after[key] for key in ("entity_id", "workspace_id", "type"))
            or (before.get("config") or {}).get("channel_config_id") != account.id
        ):
            raise BlueprintUpgradePlanChangedError("Invalid channel restore receipt.")
        if before and before.get("status") == "active" and (row.type == "slack" or row.status != "active"):
            conflict = (await db.execute(select(Channel.id).where(
                Channel.entity_id == workspace.entity_id, Channel.type == row.type,
                Channel.status == "active", Channel.config["channel_config_id"].astext == account.id,
                Channel.id != row.id,
            ).limit(1))).scalar_one_or_none()
            if conflict:
                raise BlueprintUpgradePlanChangedError("Channel routing changed after the upgrade.")
        prepared.append((row, account, before))
    return prepared


async def revert(db, *, workspace, by_user_id, actor):
    from packages.core.blueprints.component_upgrade import validate_components_restore, revert_components
    from packages.core.blueprints.configuration_upgrade import validate_configuration_restore, revert_configuration
    from packages.core.services.channel_bindings import (
        CHANNEL_BINDING_SNAPSHOT_FIELDS, preserve_channel_binding_route_order,
    )

    async with db.begin_nested():
        workspace = await _lock(db, workspace)
        state = await _state(db, workspace, lock=True)
        point = deepcopy(state.restore_point) if state else None
        if not point:
            raise BlueprintUpgradePlanChangedError("There is no applied upgrade to undo.")
        if _metadata(workspace) != point["metadata_after"]:
            raise BlueprintUpgradePlanChangedError("Workspace setup changed after the upgrade; undo would overwrite it.")
        await validate_components_restore(db, workspace=workspace, restore=point["components"], actor=actor)
        await validate_configuration_restore(db, workspace=workspace, restore=point["configuration"], actor=actor)
        channels = await _channel_restore_rows(db, workspace=workspace, entries=point["channels"])
        configuration = await revert_configuration(
            db, workspace=workspace, restore=point["configuration"], actor=actor, by_user_id=by_user_id,
        )
        components = await revert_components(
            db, workspace=workspace, restore=point["components"], actor=actor,
            by_user_id=by_user_id, validated=True,
        )
        for row, account, before in channels:
            if row.type == "twilio_voice":
                from packages.core.services.voice.call_sessions import cancel_unconnected_call_sessions_for_binding
                await cancel_unconnected_call_sessions_for_binding(
                    db, channel_config_id=account.id, channel_binding_id=row.id,
                    reason="Blueprint channel binding was reverted before the call connected.",
                )
            if before is None:
                await db.delete(row)
            else:
                for field in CHANNEL_BINDING_SNAPSHOT_FIELDS:
                    setattr(row, field, deepcopy(before[field]))
                row.user_id = account.owner_user_id
                preserve_channel_binding_route_order(row, before["updated_at"])
        _restore_metadata(workspace, point["metadata_before"])
        # Do not resurrect a previous v2 restore point or an older v3 receipt.
        settings = deepcopy(workspace.settings)
        settings[BLUEPRINT_SETTINGS_KEY].pop("restore_point", None)
        workspace.settings = settings
        state.restore_point = None
        state.baseline = point["baseline_before"] or {}
        state.revision += 1
        await db.flush()
        return {"upgrade_protocol_version": PROTOCOL_VERSION, "can_revert": False,
                "reverted": (configuration or {}).get("reverted", []) + (components or {}).get("reverted", [])}


async def invalidate_after_legacy_upgrade(db, *, workspace):
    """An old client must not leave a v3 receipt claiming a different state."""
    state = await _state(db, workspace, lock=True)
    if state is not None:
        state.restore_point = None
        state.baseline = {}
        state.revision += 1
    settings = deepcopy(workspace.settings or {})
    record = settings.setdefault(BLUEPRINT_SETTINGS_KEY, {})
    record.pop("synchronization_status", None)
    record.pop("upgrade_protocol_version", None)
    workspace.settings = settings


def _restore_embedded_tool_declarations(result: dict, source: dict) -> None:
    """Keep a retained runtime payload structurally valid without widening it."""
    embedded = source.get("embedded") or {}
    required_tools = {
        str(tool)
        for agent in embedded.get("agents") or []
        if isinstance(agent, dict)
        for tool in agent.get("tool_bindings") or []
        if str(tool)
    }
    required_tools.update(
        str(tool)
        for skill in embedded.get("skills") or []
        if isinstance(skill, dict)
        for tool in skill.get("tools") or []
        if str(tool)
    )
    if not required_tools:
        return

    contract = result.setdefault("contract", {})
    requires = contract.setdefault("requires", {})
    declared = list(requires.get("tools") or [])
    declared_set = set(declared)
    source_order = list(
        ((source.get("contract") or {}).get("requires") or {}).get("tools") or []
    )
    additions = [
        tool
        for tool in source_order
        if tool in required_tools and tool not in declared_set
    ]
    declared_set.update(additions)
    additions.extend(sorted(required_tools - declared_set))
    requires["tools"] = [*declared, *additions]


async def retained_runtime_payload(db, *, workspace, payload):
    """Validate retained requirements when an operator keeps the installed setup."""
    state = await _state(db, workspace)
    record = (workspace.settings or {}).get(BLUEPRINT_SETTINGS_KEY) or {}
    saved = record.get("upgrade_runtime_contract")
    if saved is None and state:
        saved = ((state.baseline or {}).get("runtime") or {}).get("installed")
    result = deepcopy(payload)
    if isinstance(saved, dict) and isinstance(saved.get("contract"), dict):
        result["contract"] = deepcopy(saved["contract"])
        result.setdefault("policy", {}).update({
            "post_install_checks": deepcopy(saved.get("post_install_checks", [])),
            "expected_baseline": deepcopy(saved.get("expected_baseline")),
        })
    else:
        # Legacy installations have only their durable channel/session setup
        # declarations. Never substitute the unaccepted upstream requirements.
        result["contract"] = {"requires": {}, "variables": [],
                              "channels": deepcopy(record.get("channel_requirements", [])),
                              "sessions": deepcopy(record.get("session_requirements", []))}
        result.setdefault("policy", {})["post_install_checks"] = []
    _restore_embedded_tool_declarations(result, payload)
    return result
