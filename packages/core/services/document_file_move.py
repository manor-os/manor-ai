"""Move filesystem-backed documents when their Knowledge folder changes."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from packages.core.config import get_settings
from packages.core.services.entity_fs import EntityFilesystemError, assert_entity_filesystem_ready
from packages.core.services.knowledge_visibility import normalize_rel_path


@dataclass(frozen=True)
class DocumentFileMoveResult:
    moved: bool
    old_fs_path: str | None = None
    new_fs_path: str | None = None
    reason: str | None = None


def move_document_file_to_folder(
    doc: Any,
    *,
    entity_id: str,
    target_folder_path: str | None,
) -> DocumentFileMoveResult:
    """Move a local document payload into ``target_folder_path``.

    The caller remains responsible for setting ``doc.folder_id`` and committing
    the DB session. Remote/metadata-only documents are intentionally skipped.
    """
    settings = get_settings()
    fs_path = normalize_rel_path(str(getattr(doc, "fs_path", "") or ""))
    if not settings.MANOR_FS_ENABLED:
        return DocumentFileMoveResult(False, fs_path or None, fs_path or None, "fs_disabled")
    if not fs_path:
        return DocumentFileMoveResult(False, None, None, "metadata_only")
    if getattr(doc, "file_url", None):
        return DocumentFileMoveResult(False, fs_path, fs_path, "remote_file")
    try:
        assert_entity_filesystem_ready()
    except EntityFilesystemError:
        return DocumentFileMoveResult(False, fs_path, None, "fs_unavailable")

    root = os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))
    if not os.path.isdir(root):
        return DocumentFileMoveResult(False, fs_path, None, "fs_unavailable")
    source_path = _safe_child_path(root, fs_path)
    if not source_path or not os.path.isfile(source_path):
        return DocumentFileMoveResult(False, fs_path, None, "missing_source")

    target_dir_rel = normalize_rel_path(target_folder_path or "")
    target_dir = _safe_child_path(root, target_dir_rel) if target_dir_rel else root
    if not target_dir:
        return DocumentFileMoveResult(False, fs_path, None, "invalid_target")
    os.makedirs(target_dir, exist_ok=True)

    filename = os.path.basename(fs_path) or str(getattr(doc, "name", "") or "document")
    desired_target_path = os.path.realpath(os.path.join(target_dir, filename))
    if source_path == desired_target_path:
        _refresh_document_file_metadata(doc, fs_path, source_path)
        return DocumentFileMoveResult(False, fs_path, fs_path, "already_in_place")

    target_path = _unique_destination_path(target_dir, filename)
    shutil.move(source_path, target_path)
    new_rel = normalize_rel_path(os.path.relpath(target_path, root))
    _refresh_document_file_metadata(doc, new_rel, target_path)
    return DocumentFileMoveResult(True, fs_path, new_rel)


def _safe_child_path(root: str, rel_path: str) -> str | None:
    candidate = os.path.realpath(os.path.join(root, normalize_rel_path(rel_path)))
    try:
        if os.path.commonpath([root, candidate]) != root:
            return None
    except ValueError:
        return None
    return candidate


def _unique_destination_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return candidate


def _refresh_document_file_metadata(doc: Any, fs_path: str, abs_path: str) -> None:
    stat = os.stat(abs_path)
    doc.fs_path = fs_path
    doc.name = os.path.basename(fs_path)
    doc.file_size = stat.st_size
    metadata = doc.metadata_ if isinstance(getattr(doc, "metadata_", None), dict) else {}
    updated = dict(metadata)
    integrity = dict(updated.get("file_integrity") or {})
    integrity.update({
        "status": "ok",
        "mtime_ns": getattr(stat, "st_mtime_ns", None),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    integrity.pop("recoverable", None)
    integrity.pop("error", None)
    updated["file_integrity"] = integrity
    doc.metadata_ = updated
