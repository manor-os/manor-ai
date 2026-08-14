#!/usr/bin/env python3
"""Compare two exported Blueprint payloads for semantic round-trip parity."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_DROP_KEYS = {
    "workspace_id", "entity_id", "workflow_id", "binding_id",
    "agent_id", "subscription_id", "created_at", "updated_at",
    "installed_at", "install_mode", "simulation_experience",
}


def _runtime_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Select fields reconstructable from an installed Workspace.

    Manifest discovery copy and install-only declarations are intentionally
    excluded. They must be verified from install preview/todos/check results.
    """
    manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {}
    embedded = payload.get("embedded") if isinstance(payload.get("embedded"), dict) else {}
    recipe = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else {}
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    return {
        "manifest": {"kind": manifest.get("kind")},
        "embedded": embedded,
        "recipe": {
            key: value
            for key, value in recipe.items()
            if key != "simulation_experience"
        },
        "policy": {"governance": policy.get("governance") or {}},
    }


def _normalize(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        drop_keys = set(_DROP_KEYS)
        # ``id`` is a generated database identity except for workflow steps,
        # where it is the portable graph key and must be compared.
        if parent_key != "steps":
            drop_keys.add("id")
        normalized = {
            key: _normalize(item, parent_key=key)
            for key, item in sorted(value.items())
            if key not in drop_keys
            and key not in {"_blueprint", "source_template_id"}
        }
        if parent_key == "settings" and not normalized:
            return None
        return {key: item for key, item in normalized.items() if item is not None}
    if isinstance(value, list):
        normalized = [_normalize(item, parent_key=parent_key) for item in value]
        # Collections are semantically keyed by their portable identity. Keep
        # workflow steps in declared order, but make unordered definitions
        # stable for comparison.
        if parent_key not in {"steps", "next", "true_next", "false_next"}:
            try:
                return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
            except TypeError:
                pass
        return normalized
    return value


def _normalize_installed_job_ids(source: Any, installed: Any) -> None:
    """Remove only the suffix the installer adds to a known source job id."""
    if not isinstance(source, dict) or not isinstance(installed, dict):
        return
    source_recipe = source.get("recipe")
    installed_recipe = installed.get("recipe")
    if not isinstance(source_recipe, dict) or not isinstance(installed_recipe, dict):
        return
    source_ids = {
        str(item.get("job_id"))
        for item in source_recipe.get("scheduled_jobs") or []
        if isinstance(item, dict) and item.get("job_id")
    }
    for item in installed_recipe.get("scheduled_jobs") or []:
        if not isinstance(item, dict):
            continue
        installed_id = str(item.get("job_id") or "")
        for source_id in source_ids:
            suffix = installed_id.removeprefix(source_id + "-")
            if len(suffix) == 8 and suffix.isalnum():
                item["job_id"] = source_id
                break


def compare(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    a = _normalize(_runtime_projection(left))
    b = _normalize(_runtime_projection(right))
    _normalize_installed_job_ids(a, b)
    if a == b:
        return []
    return ["Workspace runtime projections differ after generated metadata normalization"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("installed", type=Path)
    args = parser.parse_args()
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        installed = json.loads(args.installed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read JSON: {exc}", file=sys.stderr)
        return 2
    errors = compare(source, installed)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("Blueprint semantic round-trip passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
