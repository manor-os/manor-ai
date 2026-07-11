"""Document file-integrity state helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

STALE_FILE_INTEGRITY_STATUSES = {"missing", "invalid_path", "unavailable", "error"}
VECTOR_STATUS_FAILED = "failed"
VECTOR_STATUS_PENDING = "pending"


def mark_document_file_available(doc: Any, *, source: str) -> bool:
    meta = doc.metadata_ if isinstance(getattr(doc, "metadata_", None), dict) else {}
    integrity = meta.get("file_integrity")
    if not isinstance(integrity, dict):
        return False

    status = str(integrity.get("status") or "").lower()
    if status not in STALE_FILE_INTEGRITY_STATUSES and integrity.get("recoverable") is not False:
        return False

    updated_meta = dict(meta)
    updated_integrity = dict(integrity)
    updated_integrity.update({
        "status": "ok",
        "resolved_from": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    updated_integrity.pop("recoverable", None)
    updated_integrity.pop("error", None)
    updated_meta["file_integrity"] = updated_integrity
    doc.metadata_ = updated_meta
    if getattr(doc, "vector_status", None) == VECTOR_STATUS_FAILED:
        doc.vector_status = VECTOR_STATUS_PENDING
    return True


def mark_document_file_missing(doc: Any, *, source: str, trash: bool = True) -> bool:
    now = datetime.now(timezone.utc)
    meta = doc.metadata_ if isinstance(getattr(doc, "metadata_", None), dict) else {}
    updated_meta = dict(meta)
    integrity = dict(updated_meta.get("file_integrity") or {})
    integrity.update({
        "status": "missing",
        "detected_from": source,
        "recoverable": False,
        "checked_at": now.isoformat(),
    })
    updated_meta["file_integrity"] = integrity
    doc.metadata_ = updated_meta
    doc.vector_status = VECTOR_STATUS_FAILED
    if trash:
        doc.is_trashed = True
        doc.trashed_at = now
    return True
