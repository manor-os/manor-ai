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


#: Wait results a person answered; the files before one were reviewed there.
_HUMAN_WAIT_TYPES = frozenset({"approval", "input", "human_input"})
_WAIT_REVIEW_FILE_LIMIT = 8


def _step_files(result: dict, *, entity_id: str) -> list[dict]:
    """What one finished step delivered, newest first.

    The files its output hands over (its file links) when it names any:
    drafts, other formats and working notes it only saved stay out of the
    review. A step that names none shows what it saved (host receipts).
    """
    from packages.core.services.workflow_chat_projection import workflow_result_files

    named = []
    for item in workflow_result_files(result.get("output"), entity_id=entity_id):
        ref = {"document_id": item.get("document_id"), "name": item.get("name"), "fs_path": item.get("fsPath")}
        if ref["name"] == ref["document_id"]:
            ref["name"] = None  # A link without a label is named by its Document.
        ref = {key: value for key, value in ref.items() if value}
        if ref.get("document_id") or ref.get("fs_path"):
            named.append(ref)
    # A step's own result fields (poster_document_id, asset_document_ids) name what it delivered.
    output = result.get("output")
    for key, value in (output.items() if isinstance(output, dict) else ()):
        if str(key).endswith(("_document_id", "_document_ids")):
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str) and _DOCUMENT_ID.fullmatch(item.strip()) and not any(
                    ref.get("document_id") == item.strip() for ref in named
                ):
                    named.append({"document_id": item.strip()})
    if named:
        return named
    return [
        {key: item[key] for key in ("document_id", "name", "fs_path") if item.get(key)}
        for item in reversed(result.get("saved_files") or [])
        if isinstance(item, dict) and (item.get("document_id") or item.get("fs_path"))
    ]


def _step_file_refs(result: dict, *, entity_id: str) -> list[str]:
    return [str(item.get("document_id") or item.get("fs_path")) for item in _step_files(result, entity_id=entity_id)]


#: Steps whose output is what a person or an event supplied, never something the run made.
_SUPPLIED_STEP_TYPES = frozenset({"trigger", "webhook"})


def _completed_results_newest_first(run: Any, *, skip: str = "") -> list[dict]:
    snapshot = run.definition_snapshot if isinstance(getattr(run, "definition_snapshot", None), dict) else {}
    supplied = {
        str(node.get("id")) for node in snapshot.get("nodes") or []
        if isinstance(node, dict) and node.get("type") in _SUPPLIED_STEP_TYPES
    }
    step_results = run.step_results if isinstance(run.step_results, dict) else {}
    results = [
        result for sid, result in step_results.items()
        if sid != skip and sid not in supplied and isinstance(result, dict) and result.get("status") == "completed"
    ]
    return sorted(results, key=lambda result: str(result.get("completed_at") or ""), reverse=True)


def workflow_run_made_files(run: Any) -> list[dict]:
    """The files a run's steps saved or handed over, newest first, once each.

    What a run delivered when its result names no files: a run that ends on a
    project record still leads with the documents it made.
    """
    entity_id = str(run.entity_id or "")
    files: list[dict] = []
    seen: set[str] = set()
    for result in _completed_results_newest_first(run):
        for item in _step_files(result, entity_id=entity_id):
            key = str(item.get("document_id") or item.get("fs_path"))
            if key not in seen:
                seen.add(key)
                files.append(item)
    return files


#: Text a step may hand over as a package that links its deliverables.
_HANDOFF_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


async def _with_linked_files(
    db: Any, files: list[dict], *, entity_id: str, saved_refs: set[str],
) -> list[dict]:
    """Each handoff document followed by the files it links, once each.

    A step often names one package (``social-package.md``) whose links are the
    finished images; the review shows those images, not the drafts beside them.
    """
    from packages.core.services.document_service import get_document_content
    from packages.core.services.file_refs import reply_file_references

    shown, seen = [], {item["document_id"] for item in files}
    for item in files[:_WAIT_REVIEW_FILE_LIMIT]:
        shown.append(item)
        if item["document_id"] not in saved_refs and item.get("fs_path") not in saved_refs:
            continue
        if not str(item.get("name") or "").lower().endswith(_HANDOFF_TEXT_SUFFIXES):
            continue
        text = await get_document_content(db, item["document_id"], entity_id)
        refs = [ref.document_id or ref.fs_path for ref in reply_file_references(text or "", entity_id=entity_id)
                if ref.entity_id in {"", entity_id}]
        for linked in await review_files_from_refs(db, entity_id=entity_id, refs=refs):
            if linked["document_id"] in seen:
                continue
            if linked["document_id"] in saved_refs or linked.get("fs_path") in saved_refs:
                seen.add(linked["document_id"])
                shown.append(linked)
    return shown


async def workflow_wait_review_files(db: Any, run: Any, *, step_id: str) -> list[dict]:
    """The files a wait that names none puts in front of the person.

    What the run made since a person last answered it, newest first: an
    approval reviews the work it approves, a confirmation sees the file it is
    about. A wait right after another one reaches back to earlier work.
    """
    entity_id = str(run.entity_id or "")
    recent: list[str] = []
    earlier: list[str] = []
    answered = False
    results = _completed_results_newest_first(run, skip=step_id)
    for result in results:
        if str(result.get("wait_type") or "") in _HUMAN_WAIT_TYPES:
            answered = True
            continue
        (earlier if answered else recent).extend(_step_file_refs(result, entity_id=entity_id))
    files = await review_files_from_refs(db, entity_id=entity_id, refs=recent)
    if not files:
        files = await review_files_from_refs(db, entity_id=entity_id, refs=earlier)
    # Only follow links between files the host recorded this run saving.
    # A Markdown link alone does not authorize exposing another file's metadata.
    saved_refs = {
        str(item.get("document_id") or item.get("fs_path"))
        for result in results
        for item in result.get("saved_files") or ()
        if isinstance(item, dict) and (item.get("document_id") or item.get("fs_path"))
    }
    return (await _with_linked_files(
        db, files, entity_id=entity_id, saved_refs=saved_refs,
    ))[:_WAIT_REVIEW_FILE_LIMIT]


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
