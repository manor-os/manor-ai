"""Find Knowledge documents by what they say, without the paused index.

Document indexing (chunks + embeddings) is paused, so ``document_chunks`` only
covers files indexed before the pause and anything newer is findable by name
alone. This search reads document text at query time instead: the caller's
visible documents, newest first, bounded by a document count and a time
budget, with extracted text cached per file version. It needs no queue work
and no index, and it honours the same Document ACL as every other read.

Ranking is lexical: Latin words and numbers as terms, Chinese runs split into
bigrams (a Chinese question is one unbroken run, so whole-run matching never
hits), scored by how many query terms a document contains.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from typing import Any

# Documents scanned per query, newest first, and the wall-clock budget for
# reading them; a query past either reports ``partial``.
SCAN_LIMIT = 300
TIME_BUDGET_SECONDS = 6.0
_READ_CONCURRENCY = 8
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_TEXT_CHARS = 200_000
_SNIPPET_CHARS = 600

_CACHE_ENTRIES = 512
_text_cache: OrderedDict[tuple[str, float, int], str] = OrderedDict()

_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
_CJK_RUN_RE = re.compile(r"[㐀-鿿]+")
_STOP_TERMS = frozenset({
    "the", "and", "or", "for", "with", "from", "that", "this", "what", "which",
    "how", "much", "many", "our", "we", "is", "are", "of", "in", "to", "a",
    # function-word bigrams that carry no topic
    "多少", "怎么", "哪些", "哪个", "一下", "根据", "知识", "识库", "知识库",
})
# A Chinese bigram containing one of these is grammar, not topic.
_FUNCTION_CHARS = frozenset("的是我你他她它们么吗呢了在和与及请帮这那哪个有吧呀啊就都也还")


def query_terms(query: str) -> list[str]:
    """Latin words/numbers plus Chinese bigrams (single-char runs kept)."""
    text = (query or "").lower()
    terms: list[str] = []
    for token in _LATIN_RE.findall(text):
        token = token.strip("._-")
        if token and (len(token) > 1 or token.isdigit()) and token not in _STOP_TERMS:
            terms.append(token)
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            terms.append(run)
            continue
        terms.extend(
            run[i:i + 2] for i in range(len(run) - 1)
            if not (_FUNCTION_CHARS & set(run[i:i + 2]))
        )
    return [term for term in dict.fromkeys(terms) if term not in _STOP_TERMS]


def score_text(terms: list[str], name: str, text: str) -> tuple[float, list[str]]:
    """Share of query terms present (name hits count double), and the hits."""
    if not terms:
        return 0.0, []
    haystack = text.lower()
    lowered_name = (name or "").lower()
    hits = [term for term in terms if term in haystack or term in lowered_name]
    if not hits:
        return 0.0, []
    name_hits = sum(1 for term in hits if term in lowered_name)
    return round((len(hits) + 0.5 * name_hits) / len(terms), 4), hits


def snippet(text: str, hits: list[str], *, max_chars: int = _SNIPPET_CHARS) -> str:
    """The window of ``text`` holding the most query terms."""
    if len(text) <= max_chars:
        return text.strip()
    lowered = text.lower()
    best_start, best_count = 0, -1
    for term in hits[:12]:
        position = lowered.find(term)
        while position >= 0:
            start = max(0, position - max_chars // 3)
            window = lowered[start:start + max_chars]
            count = sum(1 for other in hits if other in window)
            if count > best_count:
                best_start, best_count = start, count
            position = lowered.find(term, position + 1)
            if best_count == len(hits):
                break
    piece = text[best_start:best_start + max_chars].strip()
    return f"{'…' if best_start else ''}{piece}{'…' if best_start + max_chars < len(text) else ''}"


async def _document_text(entity_root: str, doc: Any) -> str:
    """Extracted text of one document version, cached by path, mtime and size.

    Media has no extractable text: its stored description (written once by
    the background analysis) is used instead, without opening the file.
    """
    from packages.core.services.knowledge_media_analysis import stored_media_text
    from packages.core.services.media_analysis import media_kind
    from packages.core.services.text_extraction import extract_text

    if media_kind(getattr(doc, "mime_type", None), getattr(doc, "file_type", None), getattr(doc, "name", None)):
        return stored_media_text(getattr(doc, "metadata_", None))[:_MAX_TEXT_CHARS]
    fs_path = str(getattr(doc, "fs_path", "") or "")
    if not fs_path:
        return ""
    path = os.path.realpath(os.path.join(entity_root, fs_path))
    if not path.startswith(os.path.realpath(entity_root) + os.sep):
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    if stat.st_size > _MAX_FILE_BYTES:
        return ""
    key = (path, stat.st_mtime, stat.st_size)
    cached = _text_cache.get(key)
    if cached is not None:
        _text_cache.move_to_end(key)
        return cached
    try:
        text = await extract_text(
            path, mime_type=getattr(doc, "mime_type", None), file_type=getattr(doc, "file_type", None),
        )
    except Exception:
        text = ""
    text = (text or "")[:_MAX_TEXT_CHARS]
    _text_cache[key] = text
    while len(_text_cache) > _CACHE_ENTRIES:
        _text_cache.popitem(last=False)
    return text


async def search_knowledge_content(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None,
    query: str,
    limit: int = 8,
    role: str | None = None,
    actor_type: str = "agent",
    filters: dict[str, Any] | None = None,
    user_wording: str | None = None,
    scan_limit: int = SCAN_LIMIT,
    time_budget_seconds: float = TIME_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Documents whose text matches ``query``, best first, with a snippet each.

    ``user_wording`` is the user's own message for this turn. The model often
    rewrites the question as a search query, and may translate it (a Chinese
    question searched as "reimbursement deadline"); matching the user's words
    too keeps documents written in the user's language findable. Each
    document is read once and keeps the better of the two scores.

    ``actor_type`` is who reads: ``"agent"`` for the runtime, ``"user"`` when
    a signed-in user searches through the API (restricted documents stay
    agent-invisible but readable by users who may read them).
    """
    from packages.core.services.document_access import list_visible_documents
    from packages.core.services.entity_fs import get_entity_root

    term_sets = [terms for terms in (query_terms(query), query_terms(user_wording or "")) if terms]
    if not term_sets:
        return {"matches": [], "scanned": 0, "partial": False}
    docs, total = await list_visible_documents(
        db, entity_id, user_id=user_id, role=role, actor_type=actor_type,
        **{key: value for key, value in (filters or {}).items() if value is not None},
        limit=scan_limit,
    )
    # Reading file text below can take the whole time budget; holding the
    # listing's transaction across it would keep a database admission permit
    # from every other request in the process (DatabaseBusy 503s). The rows
    # stay loaded; the caller re-reads access facts before using them.
    from packages.core.database_runtime import release_read_transaction

    await release_read_transaction(db)
    entity_root = get_entity_root(entity_id)
    deadline = time.monotonic() + time_budget_seconds
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
    timed_out = False

    async def read(doc: Any) -> tuple[Any, str] | None:
        nonlocal timed_out
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                return None
            try:
                return doc, await asyncio.wait_for(_document_text(entity_root, doc), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                return None

    read_results = await asyncio.gather(*(read(doc) for doc in docs))
    matches = []
    for item in read_results:
        if item is None:
            continue
        doc, text = item
        name = getattr(doc, "name", "") or ""
        score, hits = max((score_text(terms, name, text) for terms in term_sets), key=lambda result: result[0])
        if score <= 0:
            continue
        matches.append((score, doc, snippet(text, hits) if text else ""))
    matches.sort(key=lambda entry: entry[0], reverse=True)
    scanned = sum(1 for item in read_results if item is not None)
    return {
        "matches": [{"doc": doc, "score": score, "snippet": text} for score, doc, text in matches[:limit]],
        "scanned": scanned,
        "partial": timed_out or total > len(docs),
    }


__all__ = ["query_terms", "score_text", "search_knowledge_content", "snippet"]
