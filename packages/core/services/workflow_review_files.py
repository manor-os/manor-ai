"""Reviewed files carry their Document, so every review surface can open and preview them.

Agents often report a deliverable by its path only. The review looks the
Document up by that exact path; opening it still passes the viewer's ACL.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

REVIEW_FILE_KEYS = ("artifacts", "review_artifacts", "files", "previews", "pages")


def _unlinked_files(review: Any) -> list[dict]:
    if not isinstance(review, dict):
        return []
    return [
        item for key in REVIEW_FILE_KEYS
        for item in (review.get(key) if isinstance(review.get(key), list) else [])
        if isinstance(item, dict) and not item.get("document_id")
        and isinstance(item.get("fs_path"), str) and item["fs_path"].strip()
    ]


_DOCUMENT_ID = re.compile(r"\b([0-9A-HJKMNP-TV-Z]{26})\b")


async def review_files_from_refs(db: Any, *, entity_id: str, refs: Iterable[Any]) -> list[dict]:
    """Documents for the files an agent asks a person to review, in the order cited.

    A reference may be a Document id, a viewer link or a Knowledge path. Unknown
    references are dropped; the review still shows its question.
    """
    from sqlalchemy import or_, select

    from packages.core.models.document import Document

    from packages.core.services.file_refs import parse_file_reference

    cited = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text:
            continue
        match = _DOCUMENT_ID.fullmatch(text) or (
            _DOCUMENT_ID.search(text) if text.startswith(("doc:", "document:")) else None
        )
        if match:
            cited.append(("id", match.group(1)))
            continue
        # Addresses decode once, through the shared file-reference parser.
        parsed = parse_file_reference(text, entity_id=entity_id, allow_relative=True)
        if parsed is None or parsed.entity_id not in {"", entity_id}:
            continue
        cited.append(("id", parsed.document_id) if parsed.document_id else ("path", parsed.fs_path))
    if db is None or not cited:
        return []
    ids = [value for kind, value in cited if kind == "id"]
    paths = [value for kind, value in cited if kind == "path"]
    rows = (await db.execute(select(Document.id, Document.name, Document.fs_path).where(
        Document.entity_id == entity_id, Document.is_trashed.is_(False),
        or_(Document.id.in_(ids), Document.fs_path.in_(paths)),
    ))).all()
    by_id = {row.id: row for row in rows}
    by_path = {row.fs_path: row for row in rows}
    files, seen = [], set()
    for kind, value in cited:
        row = by_id.get(value) if kind == "id" else by_path.get(value)
        if row is not None and row.id not in seen:
            seen.add(row.id)
            files.append({"document_id": row.id, "name": row.name, "fs_path": row.fs_path})
    return files


async def attach_review_document_ids(db: Any, *, entity_id: str, reviews: Iterable[Any]) -> None:
    """Fill ``document_id`` on reviewed files that name an active Document's path.

    A file a Flow cites by its bare id, viewer link or path (``"{{x.document_id}}"``)
    becomes the file itself, with its name, so the review plays or shows it
    instead of printing the reference."""
    reviews = [review for review in reviews if isinstance(review, dict)]
    for review in reviews:
        for key in REVIEW_FILE_KEYS:
            value = review.get(key)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not any(isinstance(item, str) for item in value):
                continue
            linked = []
            for item in value:
                found = await review_files_from_refs(db, entity_id=entity_id, refs=[item]) \
                    if isinstance(item, str) else []
                linked.append(found[0] if found else item)
            review[key] = linked
    files = [item for review in reviews for item in _unlinked_files(review)]
    if db is None or not files:
        return
    from sqlalchemy import select

    from packages.core.models.document import Document

    paths = {item["fs_path"].strip().lstrip("/") for item in files}
    rows = (await db.execute(select(Document.id, Document.fs_path).where(
        Document.entity_id == entity_id, Document.fs_path.in_(paths), Document.is_trashed.is_(False),
    ))).all()
    by_path = {fs_path: document_id for document_id, fs_path in rows}
    for item in files:
        document_id = by_path.get(item["fs_path"].strip().lstrip("/"))
        if document_id:
            item["document_id"] = document_id
