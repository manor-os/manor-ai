"""What changed between two Blueprint releases, and how big a version step it needs.

An installed Workspace reads the version step before it reads the changes: a
patch is something to apply, a major is something to review. So the step is
computed from the payloads, never left to whoever publishes:

    major  something an install relies on goes away or becomes required —
           a component removed, a Flow input removed or retyped, a new
           required Flow or install input, a newly required MCP server,
           Skill, Agent or channel, a record schema version raised, a newer
           payload format or minimum Manor version
    minor  something new an install can use — a component or optional input
           added, a newly used platform tool
    patch  anything else that changes the installable content (prompts,
           steps, copy, presentation)

The comparison is by component identity (``kind:key``), the same identity the
upgrade merges by, so "removed" here means exactly what an upgrade would
retire.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

LEVELS = ("patch", "minor", "major")


def _rank(level: str) -> int:
    return LEVELS.index(level)


def _components(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    embedded = payload.get("embedded") or {}
    recipe = payload.get("recipe") or {}
    found: dict[str, dict[str, Any]] = {}
    for kind, values in (
        ("skill", embedded.get("skills") or []),
        ("agent", embedded.get("agents") or []),
        ("workflow", recipe.get("workflows") or []),
    ):
        for spec in values:
            if not isinstance(spec, dict):
                continue
            key = str(spec.get("component_id") or spec.get("id") or spec.get("slug") or "").strip()
            if key:
                found[f"{kind}:{key}"] = spec
    return found


def _inputs(items: Any) -> dict[str, dict[str, Any]]:
    return {str(item["key"]): item for item in items or [] if isinstance(item, dict) and item.get("key")}


def _input_type(item: Mapping[str, Any]) -> Any:
    schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
    return schema.get("type") or item.get("type") or "string"


def _required(item: Mapping[str, Any]) -> bool:
    return bool(item.get("required")) and item.get("default") in (None, "")


def _record_schema_versions(payload: Mapping[str, Any]) -> dict[str, int]:
    versions: dict[str, int] = {}
    for spec in (payload.get("recipe") or {}).get("workflows") or []:
        for step in (spec or {}).get("steps") or []:
            config = (step or {}).get("config") or {}
            if (step or {}).get("type") != "workflow_project" or not config.get("project_type"):
                continue
            try:
                version = int(config.get("schema_version") or 1)
            except (TypeError, ValueError):
                continue
            project_type = str(config["project_type"])
            versions[project_type] = max(versions.get(project_type, 1), version)
    return versions


def _required_names(payload: Mapping[str, Any], field: str) -> set[str]:
    requires = (payload.get("contract") or {}).get("requires") or {}
    names = set()
    for item in requires.get(field) or []:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and not item.get("optional"):
            name = item.get("slug") or item.get("name") or item.get("id") or item.get("server") or item.get("key")
            if name:
                names.add(str(name))
    return names


def _required_channels(payload: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("channel_type") or item.get("provider") or item.get("key"))
        for item in (payload.get("contract") or {}).get("channels") or []
        if isinstance(item, dict) and item.get("required")
    }


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = []
    for part in str(value or "").split(".")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def diff_releases(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    """Component-level changes from ``old`` to ``new`` and the version step they need.

    ``reasons`` lists every finding that sets the step, most significant first,
    so a refused publish can say why. With no ``old`` (a first release) the
    step is ``patch`` and every component is added.
    """
    old = old or {}
    reasons: list[tuple[str, str]] = []

    def note(level: str, text: str) -> None:
        reasons.append((level, text))

    before, after = _components(old), _components(new)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if _canonical(before[key]) != _canonical(after[key]))
    if old:
        for key in removed:
            note("major", f"{key} was removed")
        for key in added:
            note("minor", f"{key} was added")

    for key in sorted(set(before) & set(after)):
        if not key.startswith("workflow:"):
            continue
        was, now = _inputs(before[key].get("run_inputs")), _inputs(after[key].get("run_inputs"))
        for name in sorted(set(was) - set(now)):
            note("major", f"{key} no longer takes the input {name!r}")
        for name in sorted(set(now) - set(was)):
            if _required(now[name]):
                note("major", f"{key} now requires the input {name!r}")
            else:
                note("minor", f"{key} takes a new optional input {name!r}")
        for name in sorted(set(was) & set(now)):
            if _input_type(was[name]) != _input_type(now[name]):
                note("major", f"{key} input {name!r} changed type")
            elif _required(now[name]) and not _required(was[name]):
                note("major", f"{key} input {name!r} became required")

    if old:
        was_vars = _inputs((old.get("contract") or {}).get("variables"))
        now_vars = _inputs((new.get("contract") or {}).get("variables"))
        for name in sorted(set(now_vars) - set(was_vars)):
            if _required(now_vars[name]):
                note("major", f"installing now requires the value {name!r}")
            else:
                note("minor", f"installing takes a new optional value {name!r}")

        for field in ("mcp_servers", "skills", "agents"):
            for name in sorted(_required_names(new, field) - _required_names(old, field)):
                note("major", f"now requires {field[:-1].replace('_', ' ')} {name!r}")
        for name in sorted(_required_names(new, "tools") - _required_names(old, "tools")):
            note("minor", f"now uses the platform tool {name!r}")
        for name in sorted(_required_channels(new) - _required_channels(old)):
            note("major", f"now requires a {name} channel")

        was_records, now_records = _record_schema_versions(old), _record_schema_versions(new)
        for project_type in sorted(set(was_records) & set(now_records)):
            if now_records[project_type] > was_records[project_type]:
                note("major", f"{project_type} records move to schema version {now_records[project_type]}")

        was_format = (old.get("manifest") or {}).get("blueprint_version")
        now_format = (new.get("manifest") or {}).get("blueprint_version")
        if was_format and now_format and _version_tuple(now_format) > _version_tuple(was_format):
            note("major", f"payload format {was_format} → {now_format}")
        was_min = ((old.get("contract") or {}).get("requires") or {}).get("manor_min_version")
        now_min = ((new.get("contract") or {}).get("requires") or {}).get("manor_min_version")
        if now_min and _version_tuple(now_min) > _version_tuple(was_min):
            note("major", f"needs Manor {now_min} or newer")

    level = max((level for level, _text in reasons), key=_rank, default="patch")
    ordered = sorted(reasons, key=lambda item: -_rank(item[0]))
    return {
        "min_bump": level,
        "reasons": [text for _level, text in ordered],
        "components": {"added": added, "removed": removed, "changed": changed},
    }


def minimum_bump(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> str:
    return diff_releases(old, new)["min_bump"]
