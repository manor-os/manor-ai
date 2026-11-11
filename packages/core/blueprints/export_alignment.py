"""Lay a Workspace export out like the release the Workspace was installed from.

Teams keep a Blueprint in git and develop it in Workspaces installed from it:
each person exports their Workspace over the file and merges with git. That
only works if an export differs from its release by the person's edits alone.
Three things otherwise show up as edits nobody made:

* order — lists come out in database order, so an untouched Workflow or
  service moves and a line diff (and every merge) sees churn;
* listing copy — author, summary, tags, cover and the like describe the
  Blueprint, not the Workspace, so a Workspace has none to export;
* unbound channel requirements — a release can require a channel account the
  local Workspace never connected, and an export only sees connected ones.

``align_export_to_release`` keeps the release's order for items it already
had (new items follow, in export order), takes listing copy the export left
empty from the release, and keeps a release channel requirement the export
lacks while its service still exists. Content the person changed is never
touched.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

_ITEM_KEYS = (
    "slug", "server_slug", "component_id", "key", "service_key", "goal_key", "rule_key", "process_key",
    "job_id", "workflow_slug", "library_key", "id", "name",
)

# Manifest fields that describe the listing rather than the Workspace.
_LISTING_FIELDS = (
    "author", "summary", "description", "tags", "cover_image_url", "slug", "use_when",
    "outcome_summary", "showcase_assets", "category", "changelog", "i18n",
)


def _empty(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_empty(item) for item in value.values())
    return value is None or value == "" or value == []


def _item_key(items: list[Any]) -> str | None:
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for key in _ITEM_KEYS:
        values = [item.get(key) for item in items]
        if all(isinstance(value, str) and value for value in values) and len(set(values)) == len(values):
            return key
    return None


def _align(exported: Any, release: Any) -> Any:
    if isinstance(exported, dict) and isinstance(release, dict):
        return {name: _align(value, release.get(name)) for name, value in exported.items()}
    if isinstance(exported, list) and isinstance(release, list):
        key = _item_key(exported)
        if key is None or _item_key(release) != key:
            return exported
        released = {item[key]: item for item in release}
        position = {item[key]: index for index, item in enumerate(release)}
        kept = sorted((item for item in exported if item[key] in position), key=lambda item: position[item[key]])
        added = [item for item in exported if item[key] not in position]
        return [_align(item, released.get(item[key])) for item in [*kept, *added]]
    return exported


def _services(payload: Mapping[str, Any]) -> set[str]:
    recipe = payload.get("recipe") or {}
    model = recipe.get("operating_model") or {}
    keys = {str(item.get("service_key")) for item in recipe.get("subscriptions") or [] if isinstance(item, dict)}
    keys |= {str(item.get("key")) for item in model.get("services") or [] if isinstance(item, dict)}
    return keys


def align_export_to_release(exported: dict[str, Any], release: Mapping[str, Any] | None) -> dict[str, Any]:
    """``exported`` laid out like ``release``; a copy, never the input."""
    if not isinstance(release, Mapping) or not release:
        return exported
    aligned = _align(copy.deepcopy(exported), copy.deepcopy(dict(release)))

    manifest = aligned.get("manifest")
    released_manifest = release.get("manifest") or {}
    if isinstance(manifest, dict):
        for field in _LISTING_FIELDS:
            if field in released_manifest and _empty(manifest.get(field)) and not _empty(released_manifest[field]):
                manifest[field] = copy.deepcopy(released_manifest[field])

    contract = aligned.get("contract")
    if isinstance(contract, dict):
        channels = list(contract.get("channels") or [])
        identity = lambda item: (item.get("channel_type"), item.get("linked_service_key"))  # noqa: E731
        present = {identity(item) for item in channels if isinstance(item, dict)}
        services = _services(aligned)
        for item in (release.get("contract") or {}).get("channels") or []:
            if not isinstance(item, dict) or identity(item) in present:
                continue
            if item.get("linked_service_key") and item["linked_service_key"] not in services:
                continue
            channels.append(copy.deepcopy(item))
        if channels:
            contract["channels"] = channels
    return aligned
