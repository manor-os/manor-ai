"""Canonical generated-file references shared by Tasks and Chat.

Producer tool payloads use several equivalent address fields (``fs_path``,
``path``, ``url``), while ``document_id`` is the only accepted Document
identity. This module normalizes addresses, assigns the canonical ``open_url``
consumed by clients, and deduplicates references by durable document or
entity-filesystem identity.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit


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
_PREVIEW_URL_KEYS = ("previewUrl", "preview_url")
_NAME_KEYS = ("name", "filename", "file_name", "original_name", "title")
_LOCAL_MACHINE_PATH_RE = re.compile(
    r"^(?:~/|/(?:Users|Volumes|home|root|private|tmp|var|etc)/|[A-Za-z]:[\\/])",
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
    path = parsed.path or ""
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
    # Decode URL components once, then treat the resulting path literally.
    normalized = _normalized_relative_path(unquote(rel_path))
    return (unquote(entity_id), normalized) if normalized else None


def entity_id_from_file_reference(value: Any) -> str:
    parts = _platform_fs_parts(value)
    return parts[0] if parts else ""


def _normalized_relative_path(text: str) -> str:
    path = text.replace("\\", "/")
    if _LOCAL_MACHINE_PATH_RE.match(path):
        return ""
    is_absolute_route = path.startswith("/")
    normalized = os.path.normpath(path.lstrip("/")).replace(os.sep, "/")
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        return ""
    if is_absolute_route and normalized.startswith(("viewer/", "api/v1/", "documents/")):
        return ""
    return normalized


def canonical_fs_path(value: Any) -> str:
    """Normalize literal storage paths; decode only filesystem URL components."""
    text = _text(value)
    if not text:
        return ""
    platform = _platform_fs_parts(text)
    if platform:
        return platform[1]
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        return ""
    # A raw filename can contain #, ?, or literal percent-encoded-looking text.
    return _normalized_relative_path(text)


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


def canonical_document_id(value: Any) -> str:
    """Reject filenames/URLs in the field reserved for opaque Document keys."""
    text = _text(value)
    return text if re.fullmatch(r"[A-Za-z0-9_-]+", text) else ""


def _document_id(ref: dict[str, Any]) -> str:
    return canonical_document_id(ref.get("document_id"))


def generated_file_ref_url_keys(ref: dict[str, Any]) -> tuple[str, ...]:
    """A preview is an address fallback, never an alias of a known file."""
    if (
        _document_id(ref)
        or any(canonical_fs_path(ref.get(key)) for key in _FS_PATH_KEYS)
        or _first_text(ref, _URL_KEYS)
    ):
        return _URL_KEYS
    return _PREVIEW_URL_KEYS


def _ref_fs_path(ref: dict[str, Any]) -> str:
    for key in _FS_PATH_KEYS:
        path = canonical_fs_path(ref.get(key))
        if path:
            return path
    for key in generated_file_ref_url_keys(ref):
        parts = _platform_fs_parts(ref.get(key))
        if parts:
            return parts[1]
    return ""


def _ref_entity_id(ref: dict[str, Any], fallback: Any = None) -> str:
    entity = _text(fallback or ref.get("entity_id") or ref.get("entityId"))
    if entity:
        return entity
    for key in generated_file_ref_url_keys(ref):
        entity = entity_id_from_file_reference(ref.get(key))
        if entity:
            return entity
    return ""


def _external_url(ref: dict[str, Any]) -> str:
    for key in generated_file_ref_url_keys(ref):
        value = _text(ref.get(key))
        if value.startswith(("http://", "https://", "data:", "blob:")):
            return value
    return ""


def _canonical_viewer_address(value: Any) -> str:
    # Only our relative viewer route is a Document address. An external site's
    # /viewer/<id> must not acquire a local Document identity.
    match = re.fullmatch(r"/viewer/([^/?#]+)/?(?:[?#].*)?", _text(value))
    document_id = canonical_document_id(unquote(match[1])) if match else ""
    return f"/viewer/{document_id}" if document_id else ""


@dataclass(frozen=True)
class ArtifactReferenceIdentity:
    """Canonical identities for one producer reference."""

    document_id: str
    fs_path: str
    entity_id: str


class ArtifactReferenceFactory:
    """Create canonical generated-file refs from heterogeneous tool output."""

    def __init__(self, *, entity_id: Any = None) -> None:
        self.entity_id = entity_id

    def inspect(self, raw_ref: dict[str, Any]) -> ArtifactReferenceIdentity:
        return ArtifactReferenceIdentity(
            document_id=_document_id(raw_ref),
            fs_path=_ref_fs_path(raw_ref),
            entity_id=_ref_entity_id(raw_ref, self.entity_id),
        )

    def create(self, raw_ref: dict[str, Any]) -> dict[str, Any]:
        """Normalize one producer reference and assign its canonical open URL."""

        ref = dict(raw_ref)
        # Never trust a producer-authored display link. Rebuild it from the
        # canonical identity below so label and destination cannot disagree.
        ref.pop("markdown_link", None)
        identity = self.inspect(ref)

        if identity.document_id:
            ref["document_id"] = identity.document_id
        else:
            ref.pop("document_id", None)
        # Do not preserve an invalid viewer address as an explicit fallback.
        for key in (*_URL_KEYS, *_PREVIEW_URL_KEYS):
            address = _text(ref.get(key))
            if address.startswith("/viewer/") and not _canonical_viewer_address(address):
                ref.pop(key, None)
        if identity.fs_path:
            ref["fs_path"] = identity.fs_path

        if identity.document_id:
            open_url = f"/viewer/{quote(identity.document_id, safe='')}"
            ref["viewer_url"] = open_url
        else:
            open_url = entity_fs_open_url(identity.entity_id, identity.fs_path)
            if not open_url:
                explicit = _first_text(ref, ("open_url", "openUrl", "viewer_url", "viewerUrl"))
                open_url = explicit or _external_url(ref)
            if not open_url:
                open_url = next((
                    _text(ref.get(key)) for key in generated_file_ref_url_keys(ref)
                    if _canonical_viewer_address(ref.get(key))
                ), "")
            if not open_url and identity.fs_path:
                # Legacy rows may predate the entity id on the serialized ref.
                # A trusted client can still read this path in the active entity.
                open_url = identity.fs_path
        if open_url:
            ref["open_url"] = open_url
            name = _first_text(ref, _NAME_KEYS)
            if not name and identity.fs_path:
                name = os.path.basename(identity.fs_path.rstrip("/"))
            markdown_link = canonical_file_markdown_link(name, open_url)
            if markdown_link:
                ref["markdown_link"] = markdown_link
        return ref


def canonical_generated_file_ref(
    raw_ref: dict[str, Any],
    *,
    entity_id: Any = None,
) -> dict[str, Any]:
    """Compatibility wrapper around the shared reference factory."""

    return ArtifactReferenceFactory(entity_id=entity_id).create(raw_ref)


def generated_file_ref_aliases(ref: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    identity = ArtifactReferenceFactory().inspect(ref)
    if identity.document_id:
        aliases.add(f"document:{identity.document_id}")
        aliases.add(f"open:/viewer/{identity.document_id}")
    if identity.fs_path:
        aliases.add(f"fs:{identity.fs_path}")
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
    for key in generated_file_ref_url_keys(ref):
        viewer = _canonical_viewer_address(ref.get(key))
        if viewer:
            aliases.add(f"open:{viewer}")
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
    "ArtifactReferenceFactory",
    "ArtifactReferenceIdentity",
    "canonical_document_id",
    "canonical_file_markdown_link",
    "canonical_fs_path",
    "canonical_generated_file_ref",
    "dedupe_generated_file_refs",
    "entity_fs_open_url",
    "entity_id_from_file_reference",
    "generated_file_ref_aliases",
    "generated_file_ref_url_keys",
]
