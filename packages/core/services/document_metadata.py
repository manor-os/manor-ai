"""Helpers for structured ``documents.metadata`` values.

Document table columns remain the source of truth for file identity, source,
timestamps, and storage location. Metadata is reserved for contextual
provenance, artifact role, provider/generation parameters, and external
system references.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


DOCUMENT_METADATA_SCHEMA_VERSION = 2
ORIGIN_ID_KEYS = frozenset({
    "workspace_id",
    "task_id",
    "agent_id",
    "conversation_id",
    "user_id",
})


def _clean_mapping(values: dict[str, Any] | None) -> dict[str, Any]:
    """Drop empty values while preserving falsey-but-meaningful values."""
    if not values:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict):
            nested = _clean_mapping(value)
            if nested:
                cleaned[key] = nested
            continue
        cleaned[key] = value
    return cleaned


def merge_document_metadata(
    metadata: dict[str, Any] | None = None,
    *,
    origin: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
    generation: dict[str, Any] | None = None,
    external: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge standard metadata sections without duplicating document columns.

    Sections:
    - origin: runtime context such as workspace/task/agent/conversation/tool.
    - artifact: product role such as final/draft/intermediate and storage scope.
    - generation: prompt/model/provider parameters used to create generated media.
    - external: source identifiers/URLs from external systems.
    """
    meta = deepcopy(metadata or {}) if isinstance(metadata, dict) else {}
    meta["schema_version"] = DOCUMENT_METADATA_SCHEMA_VERSION
    legacy_origin = _clean_mapping({
        key: meta.pop(key, None)
        for key in ORIGIN_ID_KEYS
        if key in meta
    })
    for section, values in (
        ("origin", {**legacy_origin, **(origin or {})}),
        ("artifact", artifact),
        ("generation", generation),
        ("external", external),
    ):
        cleaned = _clean_mapping(values)
        if not cleaned:
            continue
        existing = meta.get(section)
        current = dict(existing) if isinstance(existing, dict) else {}
        current.update(cleaned)
        meta[section] = current
    if extra:
        cleaned_extra = _clean_mapping(extra)
        extra_origin = {
            key: cleaned_extra.pop(key)
            for key in list(cleaned_extra)
            if key in ORIGIN_ID_KEYS
        }
        if extra_origin:
            existing = meta.get("origin")
            current = dict(existing) if isinstance(existing, dict) else {}
            for key, value in extra_origin.items():
                current.setdefault(key, value)
            meta["origin"] = current
        meta.update(cleaned_extra)
    return meta


def metadata_origin(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return the canonical structured origin block."""
    meta = metadata if isinstance(metadata, dict) else {}
    raw_origin = meta.get("origin") if isinstance(meta, dict) else {}
    return dict(raw_origin) if isinstance(raw_origin, dict) else {}


def metadata_artifact(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return new structured artifact info with legacy top-level compatibility."""
    meta = metadata if isinstance(metadata, dict) else {}
    raw_artifact = meta.get("artifact") if isinstance(meta, dict) else {}
    artifact = dict(raw_artifact) if isinstance(raw_artifact, dict) else {}
    if "artifact_role" in meta and "role" not in artifact:
        artifact["role"] = meta["artifact_role"]
    if "storage_scope" in meta and "storage_scope" not in artifact:
        artifact["storage_scope"] = meta["storage_scope"]
    return artifact
