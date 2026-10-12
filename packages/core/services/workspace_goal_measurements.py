"""Resolve confirmed draft measurements into portable Workspace Stat definitions."""
from __future__ import annotations

import copy
from typing import Any

from packages.core.constants.workspace_drafts import WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD
from packages.core.goals.scheduling import validate_measurement_cadence
from packages.core.stats.library import get_library_entry, list_library_entries
from packages.core.stats.service import (
    STAT_KEY_RE,
    SUPPORTED_VALUE_TYPES,
    SUPPORTED_WINDOWS,
    _validate_definition,
)


GOAL_MEASUREMENT_SCHEMA = {
    "type": "object",
    "description": (
        "User-confirmed measurement. Choose a supported internal library metric, "
        "or explicitly define a manually recorded metric with its formula/rubric "
        "in description and evidence source in source. No automatic scoring is "
        "implied by a custom definition."
    ),
    "oneOf": [
        {
            "type": "object",
            "required": ["library_key"],
            "properties": {
                "library_key": {"type": "string"},
                "key": {"type": "string", "minLength": 1, "maxLength": 100},
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "window": {"type": "string", "enum": sorted(SUPPORTED_WINDOWS)},
            },
            "additionalProperties": False,
        },
        {
            "type": "object",
            "required": ["key", "name", "description", "source", "unit", "value_type", "window"],
            "properties": {
                "key": {"type": "string", "maxLength": 100},
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                "unit": {"type": "string", "minLength": 1},
                "value_type": {"type": "string", "enum": sorted(SUPPORTED_VALUE_TYPES)},
                "window": {"type": "string", "enum": sorted(SUPPORTED_WINDOWS)},
            },
            "additionalProperties": False,
        },
    ],
}


def draft_measurement_library() -> list[dict[str, Any]]:
    """Only advertise collectors executable without an external account binding."""
    return [
        entry.to_dict() for entry in list_library_entries()
        if entry.collector_type == "workspace_internal" and entry.goal_eligible
    ]


def measurement_stat_definition(measurement: object, *, cadence: str) -> dict[str, Any]:
    from packages.core.contracts.json_schema import SchemaContractValidatorFactory

    error = next(SchemaContractValidatorFactory.build(GOAL_MEASUREMENT_SCHEMA).iter_errors(measurement), None)
    if error is not None:
        raise ValueError("Goal measurement requires a supported library_key or a manual metric with key, name, description, source, unit, value_type and window")
    cadence = validate_measurement_cadence(cadence)
    if "library_key" in measurement:
        entry = get_library_entry(measurement["library_key"])
        if entry is None or not entry.goal_eligible or entry.collector_type != "workspace_internal":
            raise ValueError("Choose a supported internal measurement library metric, or confirm manual recording and its evidence source")
        key = measurement.get("key", entry.key).strip()
        name = measurement.get("name", entry.name).strip()
        if not STAT_KEY_RE.fullmatch(key) or not name:
            raise ValueError("Goal measurement requires a valid lowercase Stat key and nonempty name")
        return {
            "library_key": entry.key,
            "key": key,
            "name": name,
            "window": measurement.get("window") or entry.default_window,
            "collection_cadence": cadence,
        }
    for field in ("key", "name", "description", "source", "unit"):
        if not measurement[field].strip():
            raise ValueError(f"Goal measurement {field} is required")
    key = measurement["key"].strip()
    if not STAT_KEY_RE.fullmatch(key):
        raise ValueError("Goal measurement key must be a valid lowercase Stat key")
    return {
        "key": key,
        "name": measurement["name"].strip(),
        "description": measurement["description"].strip(),
        "unit": measurement["unit"].strip(),
        "value_type": measurement["value_type"],
        "window": measurement["window"],
        "collector_type": "manual",
        "collector_config": {"source": measurement["source"].strip()},
        "collection_cadence": cadence,
        "goal_eligible": True,
    }


def resolve_draft_goal_measurements(fields: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Validate before creating any Workspace rows; never synthesize observations."""
    goals = copy.deepcopy(fields.get("goals") or [])
    definitions = fields.get("stats") or []
    if not isinstance(goals, list) or not isinstance(definitions, list):
        raise ValueError("Workspace goals and stats must be lists")
    stats: dict[str, dict] = {}
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError("Workspace Stat definitions must be objects")
        key = str(definition.get("key") or definition.get("library_key") or "").strip()
        if not key or key in stats:
            raise ValueError("Workspace Stat definitions require unique keys")
        definition = copy.deepcopy(definition)
        definition["key"] = key
        library_key = definition.get("library_key")
        entry = get_library_entry(library_key) if library_key else None
        if library_key and entry is None:
            raise ValueError(f"Unknown Stat library metric {library_key}")
        _validate_definition(
            key=key,
            value_type=entry.value_type if entry else definition.get("value_type", "number"),
            collector_type=entry.collector_type if entry else definition.get("collector_type", "manual"),
            window=definition.get("window") or (entry.default_window if entry else "latest"),
        )
        if not entry and not str(definition.get("name") or "").strip():
            raise ValueError(f"Stat {key} requires a name")
        if definition.get("collection_cadence"):
            validate_measurement_cadence(definition["collection_cadence"])
        stats[key] = definition

    version = fields.get(WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD, 0)
    requires_measurement = isinstance(version, int) and version >= 3
    for goal in goals:
        if not isinstance(goal, dict):
            raise ValueError("Workspace goals must be structured objects")
        if "measurement" in goal:
            definition = measurement_stat_definition(
                goal["measurement"],
                cadence=goal.get("cadence") or goal.get("measurement_cadence") or "",
            )
            key = definition["key"]
            if key in stats and stats[key] != definition:
                raise ValueError(f"Conflicting measurement definitions for {key}")
            stats[key] = definition
            goal["stat_key"] = key
            goal["metric_key"] = key
        stat_key = goal.get("stat_key")
        if stat_key:
            if stat_key not in stats:
                raise ValueError(f"Goal {goal.get('goal_key')} references missing Stat {stat_key}")
            definition = stats[stat_key]
            entry = get_library_entry(definition["library_key"]) if definition.get("library_key") else None
            if definition.get("goal_eligible") is False or (entry and not entry.goal_eligible):
                raise ValueError(f"Stat {stat_key} is not eligible for Goals")
            if len(stat_key) > 100:
                raise ValueError("Goal measurement keys must be at most 100 characters")
            if requires_measurement and not entry and definition.get("collector_type", "manual") == "manual":
                config = definition.get("collector_config") or {}
                measurement_stat_definition({
                    **{field: definition.get(field) for field in (
                        "key", "name", "description", "unit", "value_type", "window",
                    )},
                    "source": config.get("source") if isinstance(config, dict) else None,
                }, cadence=definition.get("collection_cadence") or "manual")
            goal["metric_key"] = stat_key
            if not goal.get("cadence") and not goal.get("measurement_cadence"):
                goal["cadence"] = definition.get("collection_cadence") or (
                    entry.default_cadence if entry and "collection_cadence" not in definition else None
                ) or "manual"
        elif requires_measurement:
            raise ValueError(f"Goal {goal.get('goal_key')} needs a confirmed measurement definition and evidence source")
    return goals, list(stats.values())


async def materialize_draft_stats(
    db, *, entity_id: str, workspace_id: str, definitions: list[dict], goal_stat_keys: set[str],
) -> dict[str, Any]:
    from packages.core.stats.service import create_stat, create_stat_from_library

    stats = {}
    for definition in definitions:
        payload = copy.deepcopy(definition)
        # Goal collectors follow the runtime switch. Standalone Blueprint
        # Stats keep their existing independent collection schedule semantics.
        install_schedule = payload["key"] not in goal_stat_keys
        if payload.get("library_key"):
            stat = await create_stat_from_library(
                db, entity_id=entity_id, workspace_id=workspace_id,
                library_key=payload["library_key"], key=payload.get("key"),
                name=payload.get("name"), window=payload.get("window"),
                collector_overrides=payload.get("collector_config"),
                origin="ai", install_schedule=install_schedule,
                **({"collection_cadence": payload["collection_cadence"]} if "collection_cadence" in payload else {}),
            )
        else:
            stat = await create_stat(
                db, entity_id=entity_id, workspace_id=workspace_id,
                key=payload["key"], name=payload["name"],
                description=payload.get("description"), unit=payload.get("unit"),
                value_type=payload.get("value_type", "number"), window=payload.get("window", "latest"),
                collector_type=payload.get("collector_type", "manual"),
                collector_config=payload.get("collector_config"),
                collection_cadence=payload.get("collection_cadence"),
                freshness_limit_seconds=payload.get("freshness_limit_seconds"),
                goal_eligible=payload.get("goal_eligible", True), origin="ai", install_schedule=install_schedule,
            )
        stats[stat.key] = stat
    return stats
