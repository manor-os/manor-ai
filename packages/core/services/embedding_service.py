"""Embedding service — generate embeddings and perform vector search via pgvector.

Uses OpenAI-compatible /embeddings endpoint (OpenRouter, OpenAI, or any compatible provider).
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re

import httpx
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document, VectorStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_MAX_CHUNK_CHARS = 24_000  # ~8 000 tokens at ~3 chars/token (OpenAI/large-context models)
_OLLAMA_CHUNK_CHARS = 400  # Conservative limit for Ollama models (512-token context, CJK ≈ 1 token/char)


class DocumentContentUnavailable(RuntimeError):
    """Raised when a filesystem-backed document cannot provide real content."""

    def __init__(self, *, status: str, fs_path: str, path: str | None = None, error: str | None = None):
        self.status = status
        self.fs_path = fs_path
        self.path = path
        self.error = error
        detail = f"{status}: {fs_path}"
        if path:
            detail = f"{detail} ({path})"
        super().__init__(detail)


def _get_embedding_config() -> dict:
    """Resolve embedding API configuration.

    Priority:
      1. Explicit EMBEDDING_API_KEY (deployment override — any provider)
      2. OPENAI_API_KEY with OpenAI text-embedding-3-small (cheap API)
      3. Ollama at EMBEDDING_BASE_URL (free, self-hosted)
      4. Empty (skip embeddings gracefully)
    """
    from packages.core.config import get_settings
    settings = get_settings()
    embedding_dimensions = int(
        os.getenv("EMBEDDING_DIMENSIONS", str(settings.EMBEDDING_DIMENSIONS)) or settings.EMBEDDING_DIMENSIONS
    )
    embedding_base_url = (os.getenv("EMBEDDING_BASE_URL") or settings.EMBEDDING_BASE_URL).rstrip("/")
    embedding_model = os.getenv("EMBEDDING_MODEL") or settings.EMBEDDING_MODEL
    embedding_api_key = os.getenv("EMBEDDING_API_KEY") or settings.EMBEDDING_API_KEY

    # 1) Explicit embedding API key takes highest priority
    if embedding_api_key:
        return {
            "api_key": embedding_api_key,
            "base_url": embedding_base_url,
            "model": embedding_model,
            "dimensions": embedding_dimensions,
        }

    # 2) OpenAI API key → use text-embedding-3-small (~$0.02/1M tokens)
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        return {
            "api_key": openai_key,
            "base_url": "https://api.openai.com/v1",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
        }

    # 3) No explicit key — will try Ollama at runtime (free local)
    return {
        "api_key": "",
        "base_url": embedding_base_url,
        "model": embedding_model,
        "dimensions": embedding_dimensions,
    }


def get_embedding_dimensions() -> int:
    """Return the configured embedding vector dimension."""
    return _get_embedding_config()["dimensions"]


def _has_embedding_key() -> bool:
    """Return True if an explicit embedding API key is configured."""
    return bool(_get_embedding_config()["api_key"])


async def _try_ollama(base_url: str) -> bool:
    """Check if Ollama is reachable."""
    url = base_url.rstrip("/")
    # Strip /v1 suffix to hit Ollama's root health endpoint
    root = url.replace("/v1", "").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(root)
            return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------


async def _resolve_embedding_config() -> dict | None:
    """Resolve a working embedding config, trying Ollama if no API key.

    Returns config dict or None if no embedding provider is available.
    """
    cfg = await _resolve_embedding_config_from_billing_context()
    if cfg:
        return cfg

    cfg = _get_embedding_config()
    if cfg["api_key"]:
        return cfg

    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud":
        try:
            from packages.core.services.model_gateway import resolve_gateway_credential

            credential = await resolve_gateway_credential(
                "openai",
                reason="embedding.official_provider_key",
            )
            if credential and credential.api_key:
                return {
                    "api_key": credential.api_key,
                    "base_url": credential.base_url.rstrip("/"),
                    "model": "text-embedding-3-small",
                    "dimensions": 1536,
                }
        except Exception:
            logger.debug("embedding official provider key lookup failed", exc_info=True)

    # No API key — try Ollama (free, at configured base URL)
    ollama_url = cfg["base_url"] or "http://localhost:11434/v1"
    if await _try_ollama(ollama_url):
        logger.info("Using local Ollama for embeddings at %s", ollama_url)
        return {
            "api_key": "ollama",  # Ollama doesn't need a real key
            "base_url": ollama_url.rstrip("/"),
            "model": cfg["model"] or "mxbai-embed-large",
            "dimensions": cfg["dimensions"],
        }

    return None


async def _resolve_embedding_config_from_billing_context() -> dict | None:
    try:
        from packages.core.ai.runtime import runtime_current_billing_context
        billing = runtime_current_billing_context()
    except Exception:
        billing = None
    if not billing or not billing.entity_id:
        return None

    try:
        from packages.core.database import async_session
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )
        async with async_session() as db:
            metadata = await resolve_llm_metadata_for_user(
                "embedding",
                user_id=billing.user_id,
                entity_id=billing.entity_id,
                db=db,
            )
            if not metadata:
                return None
            model = await resolve_model_for_user(
                "embedding",
                user_id=billing.user_id,
                entity_id=billing.entity_id,
                db=db,
            )
            return {
                "api_key": metadata["llm_api_key"],
                "base_url": str(metadata.get("llm_base_url") or "https://api.openai.com/v1").rstrip("/"),
                "model": model or "text-embedding-3-small",
                "dimensions": int(os.getenv("EMBEDDING_DIMENSIONS", "1536") or 1536),
                "byok": True,
            }
    except Exception:
        logger.debug("embedding BYOK config lookup failed", exc_info=True)
        return None


async def generate_embedding(text_input: str, *, model: str | None = None) -> list[float]:
    """Generate an embedding vector for *text_input* using an OpenAI-compatible API.

    Fallback chain: OpenAI API → Ollama local → raises RuntimeError.
    If the input exceeds the model's context length, truncate and retry once.
    """
    cfg = await _resolve_embedding_config()
    if not cfg:
        raise RuntimeError("No embedding provider available (set OPENAI_API_KEY, EMBEDDING_API_KEY, or run Ollama)")

    url = f"{cfg['base_url']}/embeddings"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    current_text = text_input
    for attempt in range(3):
        payload = {
            "input": current_text,
            "model": model or cfg["model"],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 400:
                err = resp.json().get("error", {})
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                if "context length" in msg or "too long" in msg.lower():
                    # Truncate to half and retry
                    current_text = current_text[: len(current_text) // 2]
                    logger.warning("Embedding input too long, truncating to %d chars (attempt %d)", len(current_text), attempt + 1)
                    continue
            resp.raise_for_status()
            data = resp.json()

        try:
            embedding = data["data"][0]["embedding"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected embedding response: {data}") from exc

        # Bill the call when a billing context is set + provider is paid.
        await _bill_embedding(
            cfg=cfg,
            model_used=model or cfg["model"],
            usage=data.get("usage") or {},
        )
        return embedding

    raise RuntimeError(f"Embedding failed after truncation retries for text of length {len(text_input)}")


async def generate_embeddings_batch(
    texts: list[str], *, model: str | None = None
) -> list[list[float]]:
    """Batch embedding generation.

    For providers with small context windows (e.g. Ollama), falls back to
    processing texts one-by-one via generate_embedding() which handles
    truncation retries.
    """
    cfg = await _resolve_embedding_config()
    if not cfg:
        raise RuntimeError("No embedding provider available")

    is_ollama = not cfg["api_key"] or cfg["api_key"] == "ollama"

    # Ollama doesn't reliably support batch embedding — process individually
    if is_ollama:
        results: list[list[float]] = []
        for t in texts:
            emb = await generate_embedding(t, model=model)
            results.append(emb)
        return results

    url = f"{cfg['base_url']}/embeddings"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    results = []
    batch_size = 100

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        payload = {"input": batch, "model": model or cfg["model"]}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # Sort by index to guarantee order
        items = sorted(data["data"], key=lambda d: d["index"])
        results.extend(item["embedding"] for item in items)
        # Bill this batch — usage.total_tokens is summed across the batch.
        await _bill_embedding(
            cfg=cfg,
            model_used=model or cfg["model"],
            usage=data.get("usage") or {},
        )

    return results


# ---------------------------------------------------------------------------
# Document content loading
# ---------------------------------------------------------------------------


def _document_fs_candidates(doc: Document) -> list[str]:
    if not doc.fs_path:
        return []

    from packages.core.config import get_settings

    fs_path = str(doc.fs_path)
    if os.path.isabs(fs_path):
        return [fs_path]

    fs_root = get_settings().MANOR_FS_ROOT
    return [os.path.join(fs_root, doc.entity_id, fs_path)]


def _ensure_document_file_available(doc: Document) -> None:
    """Guard filesystem-backed docs before indexing state changes.

    Documents with an ``fs_path`` must be backed by a real file. External docs
    without ``fs_path`` may still use metadata/text fallbacks.
    """
    if not doc.fs_path:
        return

    candidates = _document_fs_candidates(doc)
    if any(os.path.isfile(path) for path in candidates):
        return

    path = candidates[0] if candidates else None
    status = "missing"
    if path and not os.path.isabs(str(doc.fs_path)):
        from packages.core.config import get_settings

        fs_root = get_settings().MANOR_FS_ROOT
        entity_root = os.path.join(fs_root, doc.entity_id)
        if not os.path.isdir(fs_root) or not os.path.isdir(entity_root):
            status = "unavailable"

    raise DocumentContentUnavailable(status=status, fs_path=str(doc.fs_path), path=path)


async def _read_document_content(doc: Document) -> str:
    """Read document text content from file system or metadata.

    Uses the text_extraction service for format-aware extraction (PDF, HTML, CSV, etc.).
    Filesystem-backed documents require the file to exist before metadata/name
    fallbacks are allowed.
    """
    from packages.core.services.text_extraction import extract_text

    # Try fs_path with format-aware extraction
    if doc.fs_path:
        candidates = _document_fs_candidates(doc)
        found_file = False
        for path in candidates:
            if not os.path.isfile(path):
                continue
            found_file = True
            content = await extract_text(path, mime_type=doc.mime_type, file_type=doc.file_type)
            if content:
                return content

        if not found_file:
            raise DocumentContentUnavailable(
                status="missing",
                fs_path=str(doc.fs_path),
                path=candidates[0] if candidates else None,
            )

    # Fall back only for docs without a stale filesystem pointer, or for
    # existing files whose format has no extractable text (images/video).
    if doc.metadata_:
        for key in ("content", "content_text"):
            value = doc.metadata_.get(key)
            if isinstance(value, str):
                return value[:200_000]

    return doc.name


def _with_file_integrity(metadata: dict | None, **fields: object) -> dict:
    updated = dict(metadata or {}) if isinstance(metadata, dict) else {}
    integrity = dict(updated.get("file_integrity") or {})
    integrity.update(fields)
    integrity["checked_at"] = datetime.now(timezone.utc).isoformat()
    if fields.get("status") == "ok":
        integrity.pop("recoverable", None)
        integrity.pop("error", None)
    updated["file_integrity"] = integrity
    return updated


async def _mark_document_content_unavailable(
    db: AsyncSession,
    doc: Document,
    exc: DocumentContentUnavailable,
) -> None:
    meta = dict(doc.metadata_ or {})
    meta.pop("indexing", None)
    fields: dict[str, object] = {
        "status": exc.status,
        "fs_path": exc.fs_path,
        "source": "embedding",
    }
    if exc.path:
        fields["path"] = exc.path
    if exc.error:
        fields["error"] = exc.error
    if exc.status == "missing":
        fields["recoverable"] = False

    doc.metadata_ = _with_file_integrity(meta, **fields)
    doc.vector_status = VectorStatus.PENDING if exc.status == "unavailable" else VectorStatus.SKIPPED
    await db.commit()


def _chunk_text(text_content: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks of roughly *max_chars* characters."""
    if len(text_content) <= max_chars:
        return [text_content]
    chunks = []
    start = 0
    while start < len(text_content):
        end = start + max_chars
        # Try to break at a paragraph or sentence boundary
        if end < len(text_content):
            for sep in ("\n\n", "\n", ". ", " "):
                idx = text_content.rfind(sep, start + max_chars // 2, end)
                if idx != -1:
                    end = idx + len(sep)
                    break
        chunks.append(text_content[start:end])
        start = end
    return chunks


def _average_embeddings(embeddings: list[list[float]]) -> list[float]:
    """Compute the element-wise average of multiple embedding vectors."""
    dim = len(embeddings[0])
    avg = [0.0] * dim
    for emb in embeddings:
        for i in range(dim):
            avg[i] += emb[i]
    n = len(embeddings)
    return [v / n for v in avg]


# ---------------------------------------------------------------------------
# Document indexing
# ---------------------------------------------------------------------------


async def index_document(db: AsyncSession, document_id: str) -> bool:
    """Generate embedding for a document and store it.

    1. Load document from DB
    2. Read document content
    3. Chunk if needed, generate embedding(s), average if multi-chunk
    4. Store embedding + set vector_status = 'ready'

    Returns True on success, False on failure.
    """
    result = await db.execute(select(Document).where(Document.id == document_id))
    doc = result.scalar_one_or_none()
    if not doc:
        logger.error("Document %s not found", document_id)
        return False
    if doc.is_trashed:
        logger.info("Skipping embedding for trashed document %s", document_id)
        doc.vector_status = VectorStatus.SKIPPED
        await db.commit()
        return True

    try:
        _ensure_document_file_available(doc)
    except DocumentContentUnavailable as exc:
        logger.info("Skipping embedding for document %s: %s", document_id, exc)
        await _mark_document_content_unavailable(db, doc, exc)
        return False

    async def _update_progress(step: str, progress: int, total_chunks: int = 0, current_chunk: int = 0):
        """Persist indexing progress into document metadata.

        Uses commit() so the API can see intermediate progress via polling.
        """
        meta = dict(doc.metadata_ or {})
        meta["indexing"] = {
            "step": step,
            "progress": progress,
            "total_chunks": total_chunks,
            "current_chunk": current_chunk,
        }
        doc.metadata_ = meta
        await db.commit()

    # Mark as processing
    doc.vector_status = VectorStatus.PROCESSING
    await _update_progress("starting", 0)

    # Skip embedding if no provider available — mark as "ready" (text search still works)
    cfg = await _resolve_embedding_config()
    if not cfg:
        logger.info("No embedding provider available — skipping vectorization for document %s", document_id)
        doc.vector_status = VectorStatus.READY
        await _update_progress("skipped", 100)
        return True

    try:
        await _update_progress("reading", 10)
        content = await _read_document_content(doc)

        await _update_progress("chunking", 20)
        # Use smaller chunks for Ollama (limited context window)
        chunk_size = _OLLAMA_CHUNK_CHARS if not cfg["api_key"] or cfg["api_key"] == "ollama" else _MAX_CHUNK_CHARS
        chunks = _chunk_text(content, max_chars=chunk_size)
        total = len(chunks)

        await _update_progress("embedding", 30, total_chunks=total, current_chunk=0)

        if total == 1:
            embedding = await generate_embedding(chunks[0])
            await _update_progress("embedding", 90, total_chunks=1, current_chunk=1)
        else:
            # Process in batches and report per-batch progress
            batch_size = 100
            all_embeddings: list[list[float]] = []
            for i in range(0, total, batch_size):
                batch = chunks[i : i + batch_size]
                batch_embeddings = await generate_embeddings_batch(batch)
                all_embeddings.extend(batch_embeddings)
                done = min(i + batch_size, total)
                pct = 30 + int(60 * done / total)
                await _update_progress("embedding", pct, total_chunks=total, current_chunk=done)

            embedding = _average_embeddings(all_embeddings)

        await _update_progress("storing", 95, total_chunks=total, current_chunk=total)

        # Store embedding using raw SQL since SQLAlchemy needs explicit cast for pgvector.
        # Use connection.execute() to avoid MissingGreenlet errors with asyncpg.
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        conn = await db.connection()
        await conn.execute(
            text(
                "UPDATE documents SET embedding = CAST(:vec AS vector), "
                "vector_status = :status WHERE id = :doc_id"
            ),
            {"vec": vec_str, "status": VectorStatus.READY, "doc_id": document_id},
        )

        # Sync in-memory object with what raw SQL just wrote,
        # so the subsequent flush doesn't revert vector_status to PROCESSING.
        doc.vector_status = VectorStatus.READY

        # Clear indexing progress from metadata
        meta = dict(doc.metadata_ or {})
        meta.pop("indexing", None)
        doc.metadata_ = meta
        await db.commit()

        logger.info("Indexed document %s (%d chunks)", document_id, len(chunks))
        return True

    except DocumentContentUnavailable as exc:
        logger.info("Skipping embedding for document %s: %s", document_id, exc)
        try:
            await _mark_document_content_unavailable(db, doc, exc)
        except Exception:
            logger.exception("Failed to record unavailable content for document %s", document_id)
        return False

    except Exception:
        logger.exception("Failed to index document %s", document_id)
        try:
            await db.rollback()
            doc.vector_status = VectorStatus.FAILED
            await _update_progress("failed", 0)
        except Exception:
            # Session is broken — use a fresh engine to update status
            logger.warning("Session broken for %s, using fresh connection to mark failed", document_id)
            try:
                from packages.core.database import create_worker_session
                fresh_factory = create_worker_session()
                async with fresh_factory() as fresh_db:
                    await fresh_db.execute(
                        text("UPDATE documents SET vector_status = :status WHERE id = :doc_id"),
                        {"status": VectorStatus.FAILED, "doc_id": document_id},
                    )
                    await fresh_db.commit()
            except Exception:
                logger.error("Failed to mark document %s as failed", document_id)
        return False


async def index_documents_for_entity(db: AsyncSession, entity_id: str) -> int:
    """Index all pending documents for an entity. Returns count indexed."""
    result = await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.vector_status == VectorStatus.PENDING,
            Document.is_trashed == False,  # noqa: E712
        )
    )
    docs = result.scalars().all()
    count = 0
    for doc in docs:
        if await index_document(db, doc.id):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------


_PREVIEW_MAX_CHARS = 500
_SPREADSHEET_PREVIEW_MAX_CHARS = 4_000
_LEXICAL_SCAN_MAX_CHARS = 300_000
_SPREADSHEET_FILE_TYPES = {"xlsx", "xls", "et", "csv", "tsv"}


async def _build_content_preview(
    *,
    entity_id: str,
    fs_path: str | None,
    mime_type: str | None,
    file_type: str | None,
    metadata: dict | None,
    name: str,
    max_chars: int = _PREVIEW_MAX_CHARS,
) -> str:
    """Build a human-readable preview for a document search hit.

    Uses the same format-aware extractor as indexing so PDFs/docx/etc. yield
    real text rather than raw bytes. Falls back to metadata.content_text,
    then the document name.
    """
    from packages.core.services.text_extraction import extract_text

    effective_max_chars = max_chars
    if (file_type or "").lower() in _SPREADSHEET_FILE_TYPES:
        effective_max_chars = max(max_chars, _SPREADSHEET_PREVIEW_MAX_CHARS)

    if fs_path:
        fs_root = os.getenv("MANOR_FS_ROOT", "/mnt/manor")
        candidates = [os.path.join(fs_root, entity_id, fs_path)]
        if os.path.isabs(fs_path):
            candidates.append(fs_path)
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                content = await extract_text(path, mime_type=mime_type, file_type=file_type)
            except Exception:
                logger.debug("preview extract_text failed for %s", path, exc_info=True)
                content = ""
            if content:
                return content[:effective_max_chars]

    if metadata and isinstance(metadata, dict):
        for key in ("content", "content_text"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value[:effective_max_chars]

    return name


async def search_similar(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 5,
    threshold: float = 0.7,
) -> list[dict]:
    """Semantic search using pgvector cosine similarity.

    Returns list of {document_id, name, score, content_preview}.
    """
    cfg = await _resolve_embedding_config()
    if not cfg:
        return []  # No embedding provider — return empty results
    query_embedding = await generate_embedding(query)
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    sql = text("""
        SELECT id, name, fs_path, mime_type, file_type, metadata,
               1 - (embedding <=> CAST(:query_vec AS vector)) AS score
        FROM documents
        WHERE entity_id = :eid
          AND embedding IS NOT NULL
          AND 1 - (embedding <=> CAST(:query_vec AS vector)) >= :threshold
        ORDER BY embedding <=> CAST(:query_vec AS vector)
        LIMIT :lim
    """)

    result = await db.execute(
        sql,
        {"query_vec": vec_str, "eid": entity_id, "threshold": threshold, "lim": limit},
    )
    rows = result.fetchall()

    results = []
    for row in rows:
        preview = await _build_content_preview(
            entity_id=entity_id,
            fs_path=row.fs_path,
            mime_type=row.mime_type,
            file_type=row.file_type,
            metadata=row.metadata,
            name=row.name,
        )
        results.append({
            "document_id": row.id,
            "name": row.name,
            "score": round(float(row.score), 4),
            "content_preview": preview,
        })

    return results


async def hybrid_search(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 10,
    workspace_id: str | None = None,
    group_ids: list[str] | None = None,
) -> list[dict]:
    """Hybrid search: combine vector similarity + text search.

    1. Get top N vector results (semantic)
    2. Get top N text results (ILIKE on name)
    3. Merge and deduplicate, boosting items that appear in both
    4. Return top K
    """
    allowed_doc_ids = (
        await _group_document_ids(db, entity_id, group_ids)
        if group_ids
        else await _workspace_document_ids(db, entity_id, workspace_id) if workspace_id else None
    )
    candidate_limit = limit * 4 if allowed_doc_ids is not None else max(limit * 3, 10)

    # --- Vector results ---
    # threshold=0.5 is intentionally conservative: cross-lingual embeddings
    # (e.g. mxbai-embed-large on Chinese text) produce a long tail of weak
    # 0.3–0.5 hits that drown exact filename matches in the merge step below.
    vector_results: list[dict] = []
    if await _resolve_embedding_config():
        try:
            vector_results = await search_similar(
                db, entity_id, query, limit=candidate_limit, threshold=0.5
            )
            if allowed_doc_ids is not None:
                vector_results = [r for r in vector_results if r.get("document_id") in allowed_doc_ids]
        except Exception:
            logger.warning("Vector search failed, falling back to text only", exc_info=True)

    # --- Text results ---
    from packages.core.services.document_service import list_documents

    text_docs, _ = await list_documents(
        db, entity_id, name_search=query, limit=candidate_limit,
    )
    query_lower = query.lower()
    text_results = []
    for doc in text_docs:
        if allowed_doc_ids is not None and doc.id not in allowed_doc_ids:
            continue
        preview = await _build_content_preview(
            entity_id=entity_id,
            fs_path=doc.fs_path,
            mime_type=doc.mime_type,
            file_type=doc.file_type,
            metadata=doc.metadata_,
            name=doc.name,
        )
        # name_search ILIKE also matches mime_type/source/metadata — only
        # treat a hit as "exact" when the query is literally in the name.
        name_substring_hit = bool(doc.name) and query_lower in doc.name.lower()
        text_results.append({
            "document_id": doc.id,
            "name": doc.name,
            "score": 0.9 if name_substring_hit else 0.55,
            "content_preview": preview,
        })

    lexical_results = await _lexical_scope_results(
        db,
        entity_id,
        query,
        limit=candidate_limit,
        allowed_doc_ids=allowed_doc_ids,
    )

    # --- Merge ---
    seen: dict[str, dict] = {}

    # Add vector results first (higher quality scores)
    for r in vector_results:
        seen[r["document_id"]] = r

    # Merge text results — boost score if already present from vector
    for r in text_results:
        doc_id = r["document_id"]
        if doc_id in seen:
            seen[doc_id]["score"] = min(1.0, seen[doc_id]["score"] + 0.15)
        else:
            seen[doc_id] = r

    # Merge scoped lexical results last. These are especially important for
    # Knowledge Nets when embeddings are disabled/unavailable and the natural
    # language question is longer than any filename.
    for r in lexical_results:
        doc_id = r["document_id"]
        if doc_id in seen:
            seen[doc_id]["score"] = max(float(seen[doc_id].get("score") or 0), float(r.get("score") or 0))
            if not seen[doc_id].get("content_preview") or seen[doc_id]["content_preview"] == seen[doc_id].get("name"):
                seen[doc_id]["content_preview"] = r.get("content_preview", "")
        else:
            seen[doc_id] = r

    merged = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    return merged[:limit]


async def _lexical_scope_results(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int,
    allowed_doc_ids: set[str] | None,
) -> list[dict]:
    """Content-aware fallback for scoped RAG when vector search is unavailable.

    ``list_documents(name_search=query)`` only works when the whole natural
    language query appears in metadata/name. Workspace agents usually ask a
    richer question ("brand voice safety rules draft style..."), so scoped
    Knowledge Net searches need a lightweight content scan before returning
    "no indexed content".
    """
    terms = _lexical_terms(query)
    if not terms:
        return []

    from packages.core.models.document import Document

    stmt = select(Document).where(
        Document.entity_id == entity_id,
        Document.is_trashed == False,  # noqa: E712
    )
    if allowed_doc_ids is not None:
        if not allowed_doc_ids:
            return []
        stmt = stmt.where(Document.id.in_(allowed_doc_ids))
    else:
        # Entity-wide fallback should stay bounded; scoped Knowledge Net
        # searches can safely inspect all allowed docs because the set is
        # already constrained by membership.
        stmt = stmt.limit(max(limit, 25))

    docs = list((await db.execute(stmt)).scalars().all())
    scored: list[dict] = []
    for doc in docs:
        search_content = await _build_content_preview(
            entity_id=entity_id,
            fs_path=doc.fs_path,
            mime_type=doc.mime_type,
            file_type=doc.file_type,
            metadata=doc.metadata_,
            name=doc.name,
            max_chars=_LEXICAL_SCAN_MAX_CHARS,
        )
        score = _lexical_score(query, terms, f"{doc.name}\n{search_content}")
        if score <= 0 and allowed_doc_ids is None:
            continue
        # For explicit Knowledge Net/workspace scopes, returning a small
        # low-score preview is better than a false empty result: the caller
        # intentionally selected that corpus.
        if score <= 0:
            score = 0.25
        scored.append({
            "document_id": doc.id,
            "name": doc.name,
            "score": round(score, 4),
            "content_preview": _content_snippet(search_content, terms, max_chars=1600),
        })

    return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]


def _lexical_terms(query: str) -> list[str]:
    text = query.lower()
    raw = re.findall(r"[a-z0-9_\-\u4e00-\u9fff]+", text)
    stop = {
        "the", "and", "or", "for", "with", "from", "that", "this", "into",
        "about", "rules", "rule", "criteria", "style", "draft", "drafts",
        "content", "document", "documents", "knowledge", "net",
    }
    terms: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip("-_")
        if len(token) <= 1 or token in stop or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _content_snippet(content: str, terms: list[str], *, max_chars: int = 1600) -> str:
    if len(content) <= max_chars:
        return content
    lowered = content.lower()
    matches: list[tuple[int, int, int]] = []
    for term in terms:
        if not term:
            continue
        lowered_term = term.lower()
        pos = lowered.find(lowered_term)
        if pos < 0:
            continue
        matches.append((lowered.count(lowered_term), -len(lowered_term), pos))
    if not matches:
        return content[:max_chars]
    matches.sort()
    start = max(0, matches[0][2] - max_chars // 3)
    end = min(len(content), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}"


def _lexical_score(query: str, terms: list[str], haystack: str) -> float:
    hay = haystack.lower()
    if not hay.strip():
        return 0.0
    exact = 0.25 if query.strip().lower() and query.strip().lower() in hay else 0.0
    hits = [term for term in terms if term in hay]
    if not hits:
        return exact
    coverage = len(hits) / max(len(terms), 1)
    density_bonus = min(len(hits), 6) * 0.03
    return min(0.85, 0.35 + coverage * 0.35 + density_bonus + exact)


async def _workspace_document_ids(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str | None,
) -> set[str]:
    if not workspace_id:
        return set()
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroup, DocumentGroupMember

    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        )
    )).scalars().all()
    group_ids = [
        group.id for group in groups
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if not group_ids:
        return set()
    rows = (await db.execute(
        select(DocumentGroupMember.document_id)
        .where(
            DocumentGroupMember.group_id.in_(group_ids),
        )
    )).scalars().all()
    return set(rows)


async def _group_document_ids(
    db: AsyncSession,
    entity_id: str,
    group_ids: list[str] | None,
) -> set[str]:
    clean_group_ids = [str(group_id).strip() for group_id in (group_ids or []) if str(group_id).strip()]
    if not clean_group_ids:
        return set()
    from sqlalchemy import select
    from packages.core.models.document import DocumentGroup, DocumentGroupMember

    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.id.in_(clean_group_ids),
        )
    )).scalars().all()
    allowed_group_ids = [
        group.id for group in groups
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if not allowed_group_ids:
        return set()
    rows = (await db.execute(
        select(DocumentGroupMember.document_id)
        .where(DocumentGroupMember.group_id.in_(allowed_group_ids))
    )).scalars().all()
    return set(rows)


# ── Billing ──────────────────────────────────────────────────────────

async def _bill_embedding(
    *,
    cfg: dict,
    model_used: str,
    usage: dict,
) -> None:
    """Record one embedding call against the active billing context.

    Skips silently when:
      * The provider is local Ollama (free — no API key set, mxbai-* model)
      * No billing context is set (background indexing job with no entity)
      * The response had no token usage (provider didn't return it)
    """
    # Local Ollama is free — no billing.
    if not cfg.get("api_key") or cfg["api_key"] == "ollama":
        return

    try:
        from packages.core.ai.runtime import runtime_current_billing_context
        billing = runtime_current_billing_context()
    except Exception:
        billing = None
    if billing is None or billing.suppress:
        return

    total_tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
    if total_tokens <= 0:
        return

    from packages.core.services.model_pricing_gateway import embedding_cost_usd

    cost_usd = embedding_cost_usd(model_used, total_tokens)
    if cost_usd <= 0:
        return

    try:
        from packages.core.database import async_session
        from packages.core.services.usage_service import record_media_usage
        async with async_session() as db:
            await record_media_usage(
                db,
                entity_id=billing.entity_id,
                kind="embedding",
                model=model_used,
                cost_usd=cost_usd,
                units=total_tokens,
                workspace_id=billing.workspace_id,
                user_id=billing.user_id,
                agent_id=billing.agent_id,
                conversation_id=billing.conversation_id,
                source=billing.source or "embedding",
                byok=billing.byok or bool(cfg.get("byok")),
            )
            await db.commit()
    except Exception:
        logger.debug("embedding billing failed (best-effort)", exc_info=True)
