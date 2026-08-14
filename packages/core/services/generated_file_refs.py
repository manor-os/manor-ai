"""Canonical generated-file references shared by Tasks and Chat.

Producer tool payloads use several equivalent fields (``fs_path``, ``path``,
``url``, ``document_id``).  UI surfaces must not guess which one is an
openable address.  This module normalizes those aliases, assigns the one
canonical ``open_url`` consumed by clients, and deduplicates references by
their durable document or entity-filesystem identity.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit


_DOCUMENT_ID_KEYS = ("document_id", "documentId", "doc_id", "id")
_FS_PATH_KEYS = ("fs_path", "path", "file_path", "output_path", "saved_to", "local_path")
_URL_KEYS = (
    "open_url",
    "openUrl",
    "viewer_url",
    "viewerUrl",
    "result_url",
    "file_url",
    "download_url",
    "document_url",
    "artifact_url",
    "output_url",
    "public_url",
    "url",
)
_NAME_KEYS = ("name", "filename", "file_name", "original_name", "title")
_LOCAL_MACHINE_PATH_RE = re.compile(
    r"^(?:~/|/(?:Users|Volumes|private|tmp|var|etc)/|[A-Za-z]:[\\/])",
    re.I,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(ref: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = _text(ref.get(key))
        if value:
            return value
    return ""


def _platform_fs_parts(value: Any) -> tuple[str, str] | None:
    text = _text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    path = unquote(parsed.path or "").replace("\\", "/")
    prefix = "/api/v1/fs/"
    if path.startswith(prefix):
        scoped = path[len(prefix):]
    elif path.startswith("api/v1/fs/"):
        scoped = path[len("api/v1/fs/"):]
    else:
        return None
    entity_id, separator, rel_path = scoped.partition("/")
    if not separator or not entity_id or not rel_path:
        return None
    return unquote(entity_id), rel_path.lstrip("/")


def entity_id_from_file_reference(value: Any) -> str:
    parts = _platform_fs_parts(value)
    return parts[0] if parts else ""


def canonical_fs_path(value: Any) -> str:
    """Return an entity-relative path, never a host-machine absolute path."""

    text = _text(value)
    if not text:
        return ""
    platform = _platform_fs_parts(text)
    if platform:
        return platform[1]
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        return ""
    decoded = unquote((parsed.path or text)).replace("\\", "/")
    if _LOCAL_MACHINE_PATH_RE.match(decoded):
        return ""
    is_absolute_route = decoded.startswith("/")
    normalized = os.path.normpath(decoded.lstrip("/")).replace(os.sep, "/")
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        return ""
    if is_absolute_route and normalized.startswith(("viewer/", "api/v1/", "documents/")):
        return ""
    return normalized


def entity_fs_open_url(entity_id: Any, fs_path: Any) -> str:
    entity = _text(entity_id)
    path = canonical_fs_path(fs_path)
    if not entity or not path:
        return ""
    return f"/api/v1/fs/{quote(entity, safe='')}/{quote(path, safe='/')}"


def canonical_file_markdown_link(name: Any, open_url: Any) -> str | None:
    """Return the immutable Markdown-v1 contract for an openable file.

    The destination is deliberately treated as an opaque value. Callers may
    copy this field into assistant text, but must never decode, humanize, or
    rebuild it from a filename or filesystem path.
    """

    address = _text(open_url)
    if not address.startswith(("/viewer/", "/api/v1/fs/", "http://", "https://")):
        return None
    label = _text(name) or "File"
    label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({address})"


def _document_id(ref: dict[str, Any]) -> str:
    for key in _DOCUMENT_ID_KEYS:
        value = _text(ref.get(key))
        if not value:
            continue
        if key == "id" and not (
            ref.get("mime_type")
            or ref.get("mimeType")
            or ref.get("file_type")
            or ref.get("fileType")
        ):
            continue
        return value
    return ""


def _ref_fs_path(ref: dict[str, Any]) -> str:
    for key in _FS_PATH_KEYS:
        path = canonical_fs_path(ref.get(key))
        if path:
            return path
    for key in _URL_KEYS:
        parts = _platform_fs_parts(ref.get(key))
        if parts:
            return parts[1]
    return ""


def _ref_entity_id(ref: dict[str, Any], fallback: Any = None) -> str:
    entity = _text(fallback or ref.get("entity_id") or ref.get("entityId"))
    if entity:
        return entity
    for key in _URL_KEYS:
        entity = entity_id_from_file_reference(ref.get(key))
        if entity:
            return entity
    return ""


def _external_url(ref: dict[str, Any]) -> str:
    for key in _URL_KEYS:
        value = _text(ref.get(key))
        if value.startswith(("http://", "https://", "data:", "blob:")):
            return value
    return ""


def canonical_generated_file_ref(
    raw_ref: dict[str, Any],
    *,
    entity_id: Any = None,
) -> dict[str, Any]:
    """Normalize one producer reference and assign its canonical ``open_url``."""

    ref = dict(raw_ref)
    # Never trust a producer-authored display link. Rebuild it from the
    # canonical identity below so label and destination cannot disagree.
    ref.pop("markdown_link", None)
    document_id = _document_id(ref)
    fs_path = _ref_fs_path(ref)
    scoped_entity_id = _ref_entity_id(ref, entity_id)

    if document_id:
        ref["document_id"] = document_id
    if fs_path:
        ref["fs_path"] = fs_path

    if document_id:
        open_url = f"/viewer/{quote(document_id, safe='')}"
        ref["viewer_url"] = open_url
    else:
        open_url = entity_fs_open_url(scoped_entity_id, fs_path)
        if not open_url:
            explicit = _first_text(ref, ("open_url", "openUrl", "viewer_url", "viewerUrl"))
            open_url = explicit or _external_url(ref)
        if not open_url and fs_path:
            # Legacy rows may predate the entity id on the serialized ref.  A
            # trusted client can still read this exact path in the active entity.
            open_url = fs_path
    if open_url:
        ref["open_url"] = open_url
        name = _first_text(ref, _NAME_KEYS)
        if not name and fs_path:
            name = os.path.basename(fs_path.rstrip("/"))
        markdown_link = canonical_file_markdown_link(name, open_url)
        if markdown_link:
            ref["markdown_link"] = markdown_link
    return ref


def generated_file_ref_aliases(ref: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    document_id = _document_id(ref)
    if document_id:
        aliases.add(f"document:{document_id}")
    fs_path = _ref_fs_path(ref)
    if fs_path:
        aliases.add(f"fs:{fs_path}")
    external = _external_url(ref)
    if external and not _platform_fs_parts(external):
        aliases.add(f"url:{external}")
    open_url = _first_text(ref, ("open_url", "openUrl", "viewer_url", "viewerUrl"))
    if open_url:
        platform = _platform_fs_parts(open_url)
        if platform:
            aliases.add(f"fs:{platform[1]}")
        elif not open_url.startswith("/viewer/"):
            aliases.add(f"open:{open_url}")
    return aliases


def _merge_refs(current: dict[str, Any], incoming: dict[str, Any], *, entity_id: Any) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        if value not in (None, "", [], {}) and merged.get(key) in (None, "", [], {}):
            merged[key] = value
    if not _document_id(current) and _document_id(incoming):
        merged["document_id"] = _document_id(incoming)
    return canonical_generated_file_ref(merged, entity_id=entity_id)


def dedupe_generated_file_refs(
    refs: Iterable[dict[str, Any]],
    *,
    entity_id: Any = None,
) -> list[dict[str, Any]]:
    """Merge aliases of the same file while preserving first-seen order."""

    output: list[dict[str, Any]] = []
    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            continue
        ref = canonical_generated_file_ref(raw_ref, entity_id=entity_id)
        aliases = generated_file_ref_aliases(ref)
        matching_indexes = [
            index
            for index, existing in enumerate(output)
            if aliases & generated_file_ref_aliases(existing)
        ]
        if not matching_indexes:
            output.append(ref)
            continue
        target = matching_indexes[0]
        output[target] = _merge_refs(output[target], ref, entity_id=entity_id)
        # A later enriched ref can bridge a document-only alias and a
        # filesystem-only alias that were previously separate entries.
        for duplicate in reversed(matching_indexes[1:]):
            output[target] = _merge_refs(output[target], output[duplicate], entity_id=entity_id)
            del output[duplicate]
    return output


__all__ = [
    "canonical_file_markdown_link",
    "canonical_fs_path",
    "canonical_generated_file_ref",
    "dedupe_generated_file_refs",
    "entity_fs_open_url",
    "entity_id_from_file_reference",
    "generated_file_ref_aliases",
]
