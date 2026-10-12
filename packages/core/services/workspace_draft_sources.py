"""Source materials a user attaches to a Workspace draft.

A source is an uploaded file, an existing knowledge document, or a link the
user pasted. Text is extracted once at registration and stored on
``draft.fields["sources"]`` so the architect can page through it with
``ws_read_source`` and finalize can carry it into the created Workspace.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from packages.core.models.base import generate_ulid

SOURCES_FIELD = "sources"
MAX_SOURCE_CHARS = 120_000
MAX_DRAFT_SOURCE_CHARS = 400_000
SOURCE_PREVIEW_CHARS = 1_500
STARTER_EXCERPT_CHARS = 12_000
LINK_FETCH_TIMEOUT_SECONDS = 8.0
MAX_DOCUMENT_IDS_PER_TURN = 20

# URLs end at whitespace, ASCII/full-width closers, CJK text, or CJK punctuation,
# because CJK prose is written without spaces around links.
_URL_RE = re.compile(r"https?://[^\s<>\"'）)\]】一-鿿，。；：！？、]+")
_TRAILING_PUNCT = ".,;:!?。，；：！？"


# ── pure helpers ────────────────────────────────────────────────────────────

def draft_sources(fields: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [s for s in ((fields or {}).get(SOURCES_FIELD) or []) if isinstance(s, dict)]


def find_source(fields: dict[str, Any] | None, source_id: str) -> dict[str, Any] | None:
    wanted = str(source_id or "").strip()
    for source in draft_sources(fields):
        if str(source.get("id") or "") == wanted:
            return source
    return None


def ready_source_ids(fields: dict[str, Any] | None) -> set[str]:
    return {
        str(s.get("id"))
        for s in draft_sources(fields)
        if s.get("status") == "ready" and s.get("id")
    }


def extract_links(message: str) -> list[str]:
    seen: list[str] = []
    for match in _URL_RE.finditer(message or ""):
        url = match.group(0).rstrip(_TRAILING_PUNCT)
        if url and url not in seen:
            seen.append(url)
    return seen


def strip_source_text(fields: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``fields`` with ``text`` removed from every source (API responses)."""
    if SOURCES_FIELD not in (fields or {}):
        return fields
    out = dict(fields)
    out[SOURCES_FIELD] = [
        {key: value for key, value in source.items() if key != "text"}
        for source in draft_sources(fields)
    ]
    return out


def sources_transcript_note(sources: Iterable[dict[str, Any]]) -> str:
    names = [str(s.get("name") or s.get("url") or "").strip() for s in sources]
    names = [n for n in names if n]
    return f"[附件/Attached: {', '.join(names)}]" if names else ""


def _one_line(text: str, limit: int) -> str:
    return " ".join((text or "")[:limit].split())


def sources_context_block(fields: dict[str, Any] | None, *, current_turn_index: int) -> str:
    sources = draft_sources(fields)
    if not sources:
        return ""
    lines = ["<sources>"]
    for source in sources:
        status = str(source.get("status") or "unknown")
        if status != "ready" and source.get("error"):
            status = f"{status} ({source.get('error')})"
        new_flag = "yes" if int(source.get("turn_index") or 0) == int(current_turn_index) else "no"
        lines.append(
            f"- id={source.get('id')} | kind={source.get('kind')} | name={source.get('name')} "
            f"| status={status} | chars={int(source.get('chars') or 0)} | new_this_turn={new_flag}"
        )
        preview = _one_line(str(source.get("text") or ""), SOURCE_PREVIEW_CHARS)
        if preview:
            lines.append(f"  preview: {preview}")
    lines.append(
        "Read every source fully with ws_read_source (page until next_offset is null) "
        "before designing services, skills, or knowledge from it. Sources are the user's primary brief."
    )
    lines.append("</sources>")
    return "\n".join(lines) + "\n"


def source_excerpt(
    fields: dict[str, Any] | None,
    source_ids: Iterable[str] | None,
    *,
    limit: int = STARTER_EXCERPT_CHARS,
) -> str:
    parts: list[str] = []
    remaining = max(0, int(limit))
    for source_id in dict.fromkeys(str(s).strip() for s in (source_ids or ()) if str(s).strip()):
        source = find_source(fields, source_id)
        if not source or source.get("status") != "ready":
            continue
        text = str(source.get("text") or "").strip()
        if not text or remaining <= 0:
            continue
        chunk = text[:remaining]
        remaining -= len(chunk)
        parts.append(f"## {source.get('name')}\n{chunk}")
    return "\n\n".join(parts)


# ── extraction ──────────────────────────────────────────────────────────────

async def extract_document_source(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str | None,
    document_id: str,
) -> tuple[str, str, str | None]:
    """Return ``(name, text, error)``; mirrors the chat KB-attachment extraction."""
    from packages.core.services.document_access import get_visible_document
    from packages.core.services.text_extraction import extract_text

    document = await get_visible_document(db, document_id, entity_id, user_id=user_id)
    if document is None:
        return ("", "", "not_found")
    name = str(getattr(document, "name", "") or document_id)
    if getattr(document, "is_trashed", False):
        return (name, "", "trashed")
    text = ""
    fs_path = getattr(document, "fs_path", None)
    if fs_path:
        fs_root = os.getenv("MANOR_FS_ROOT", "/mnt/manor")
        full_path = os.path.join(fs_root, entity_id, fs_path)
        try:
            text = await extract_text(
                full_path,
                mime_type=getattr(document, "mime_type", None),
                file_type=getattr(document, "file_type", None),
            ) or ""
        except Exception:  # extraction libs are best-effort
            text = ""
    if not text.strip():
        metadata = getattr(document, "metadata_", None) or {}
        for key in ("content", "content_text"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
    if not text.strip():
        return (name, "", "no_text")
    return (name, text, None)


async def fetch_link_source(url: str) -> tuple[str, str, str | None]:
    """Return ``(name, text, error)`` for a pasted link."""
    from packages.core.services.web_fetch import fetch_url

    try:
        result = await fetch_url(url, timeout=LINK_FETCH_TIMEOUT_SECONDS)
    except Exception as exc:
        return (url, "", f"fetch_failed: {exc.__class__.__name__}")
    status_code = int(getattr(result, "status_code", 0) or 0)
    if status_code >= 400:
        return (url, "", f"http_{status_code}")
    content = getattr(result, "content", b"") or b""
    content_type = str(getattr(result, "content_type", "") or "").lower()
    from packages.core.services.web_text import extract_pdf_text, html_to_text

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        text = extract_pdf_text(content)
    else:
        raw = content.decode("utf-8", errors="replace")
        head = raw[:500].lower()
        text = html_to_text(raw, as_markdown=True) if ("<html" in head or "<body" in head) else raw
    text = (text or "").strip()
    if not text:
        return (url, "", "no_text")
    return (url, text, None)


# ── registration ────────────────────────────────────────────────────────────

def _new_source(
    *,
    kind: str,
    name: str,
    text: str,
    error: str | None,
    turn_index: int,
    document_id: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    clean = (text or "").strip()
    truncated = len(clean) > MAX_SOURCE_CHARS
    clean = clean[:MAX_SOURCE_CHARS]
    source: dict[str, Any] = {
        "id": generate_ulid(),
        "kind": kind,
        "name": name or document_id or url or "source",
        "status": "failed" if error else "ready",
        "chars": len(clean),
        "text": clean,
        "turn_index": int(turn_index),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        source["error"] = error
    if truncated:
        source["truncated"] = True
    if document_id:
        source["document_id"] = document_id
    if url:
        source["url"] = url
    return source


def _budget_used(fields: dict[str, Any] | None) -> int:
    return sum(len(str(s.get("text") or "")) for s in draft_sources(fields))


def _user_turn_index(draft: Any) -> int:
    return sum(
        1
        for m in (getattr(draft, "messages", None) or [])
        if isinstance(m, dict) and m.get("role") == "user"
    )


async def register_draft_sources(
    db: AsyncSession,
    *,
    draft: Any,
    entity_id: str,
    user_id: str | None,
    message: str,
    document_ids: Iterable[str] | None,
) -> list[dict[str, Any]]:
    """Register new documents/links for this turn; returns the added entries."""
    fields = dict(draft.fields or {})
    existing = draft_sources(fields)
    known_docs = {str(s.get("document_id")) for s in existing if s.get("document_id")}
    known_urls = {str(s.get("url")) for s in existing if s.get("url")}
    turn_index = _user_turn_index(draft)
    added: list[dict[str, Any]] = []

    wanted_docs = list(dict.fromkeys(
        str(d).strip() for d in (document_ids or ()) if str(d or "").strip()
    ))[:MAX_DOCUMENT_IDS_PER_TURN]
    for document_id in wanted_docs:
        if document_id in known_docs:
            continue
        name, text, error = await extract_document_source(
            db, entity_id=entity_id, user_id=user_id, document_id=document_id,
        )
        added.append(_new_source(
            kind="document", name=name, text=text, error=error,
            turn_index=turn_index, document_id=document_id,
        ))
        known_docs.add(document_id)

    for url in extract_links(message):
        if url in known_urls:
            continue
        name, text, error = await fetch_link_source(url)
        added.append(_new_source(
            kind="link", name=name, text=text, error=error, turn_index=turn_index, url=url,
        ))
        known_urls.add(url)

    if not added:
        return []

    used = _budget_used(fields)
    for source in added:
        if source["status"] != "ready":
            continue
        if used + len(source["text"]) > MAX_DRAFT_SOURCE_CHARS:
            source.update({
                "status": "failed",
                "error": "draft_source_budget_exceeded",
                "text": "",
                "chars": 0,
            })
            source.pop("truncated", None)
            continue
        used += len(source["text"])

    fields[SOURCES_FIELD] = [*existing, *added]
    draft.fields = fields
    flag_modified(draft, "fields")
    return added


async def refresh_link_source(draft: Any, source_id: str) -> dict[str, Any] | None:
    """Re-fetch a link source in place; returns the updated entry or None."""
    fields = dict(draft.fields or {})
    source = find_source(fields, source_id)
    if source is None or source.get("kind") != "link" or not source.get("url"):
        return None
    name, text, error = await fetch_link_source(str(source["url"]))
    fresh = _new_source(
        kind="link", name=name, text=text, error=error,
        turn_index=int(source.get("turn_index") or 0), url=str(source["url"]),
    )
    fresh["id"] = source["id"]
    fresh["created_at"] = source.get("created_at") or fresh["created_at"]
    fields[SOURCES_FIELD] = [fresh if s.get("id") == source["id"] else s for s in draft_sources(fields)]
    draft.fields = fields
    flag_modified(draft, "fields")
    return fresh
