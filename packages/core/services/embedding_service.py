"""Embedding service — generate embeddings and perform vector search via pgvector.

Uses OpenAI-compatible /embeddings endpoint (OpenRouter, OpenAI, or any compatible provider).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import re
from typing import Any

import httpx
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, VectorStatus
from packages.core.cache import cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


# Retrieval-granularity chunk size, not a provider context limit. ~1600 chars
# sits in the 256-512-token range general/fact-focused RAG retrieval is
# measured to work best at — small enough that a chunk's embedding represents
# one idea, not an averaged blur of everything in the document.
_RETRIEVAL_CHUNK_CHARS = 1_600
_OLLAMA_CHUNK_CHARS = 400  # Conservative limit for Ollama models (512-token context, CJK ≈ 1 token/char)


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _nonnegative_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


# A live indexer refreshes metadata.indexing.heartbeat_epoch after every
# provider response. Keeping heartbeats out of documents.updated_at avoids
# invalidating document/thumbnail version caches once per chunk.
EMBEDDING_PROCESSING_STALE_SECONDS = _positive_int_env(
    "EMBEDDING_PROCESSING_STALE_SECONDS",
    300,
    minimum=120,
)
# Cache each provider result independently so a reclaimed document resumes by
# content hash instead of paying for or recomputing completed chunks. Keys are
# tenant-scoped and contain no raw document text.
EMBEDDING_CACHE_TTL_SECONDS = _nonnegative_int_env(
    "EMBEDDING_CACHE_TTL_SECONDS",
    86_400,
)


class _IndexingClaimLost(RuntimeError):
    """The document was cancelled, completed, or reclaimed by another run."""


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

    # 2) OpenAI API key → use text-embedding-3-small (~$0.02/1M tokens).
    # The vector column has one deployment-wide dimension; text-embedding-3-
    # small supports requesting a smaller output to honor that contract.
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if openai_key:
        return {
            "api_key": openai_key,
            "base_url": "https://api.openai.com/v1",
            "model": "text-embedding-3-small",
            "dimensions": embedding_dimensions,
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

    # Embeddings are local by default. The dev/OSS compose stack bundles
    # Ollama, so do not replace that route with a managed gateway merely
    # because the process is running with cloud features enabled.
    if not cfg["api_key"]:
        ollama_url = cfg["base_url"] or "http://localhost:11434/v1"
        if await _try_ollama(ollama_url):
            logger.info("Using local Ollama for embeddings at %s", ollama_url)
            return {
                "api_key": "ollama",  # Ollama doesn't need a real key
                "base_url": ollama_url.rstrip("/"),
                "model": cfg["model"] or "mxbai-embed-large",
                "dimensions": cfg["dimensions"],
            }

    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud":
        try:
            from packages.core.services.model_gateway import resolve_official_model_route

            route = await resolve_official_model_route(
                "openai/text-embedding-3-small",
                vercel_reason="embedding.vercel_gateway_key",
                provider_chain=("vercel", "openai", "openrouter"),
            )
            if route and route.api_key:
                return {
                    "api_key": route.api_key,
                    "base_url": route.base_url.rstrip("/"),
                    "model": "openai/text-embedding-3-small",
                    "dimensions": cfg["dimensions"],
                    "provider": route.provider,
                    "auth_method": (
                        "oidc"
                        if str(route.source_detail or "").strip()
                        == "VERCEL_OIDC_TOKEN"
                        else "api-key"
                    ),
                }
        except Exception:
            logger.debug("embedding Vercel Gateway lookup failed", exc_info=True)

    if cfg["api_key"]:
        return cfg

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
            model = await resolve_model_for_user(
                "embedding",
                user_id=billing.user_id,
                entity_id=billing.entity_id,
                db=db,
            )
            if not metadata:
                if (
                    model
                    and "/" in model
                    and os.getenv("DEPLOYMENT_MODE", "oss").strip().lower()
                    == "cloud"
                ):
                    from packages.core.services.model_gateway import (
                        resolve_official_model_route,
                    )

                    route = await resolve_official_model_route(
                        model,
                        vercel_reason="embedding.vercel_gateway_key",
                        provider_chain=("vercel", "openai", "openrouter"),
                    )
                    if route and route.api_key:
                        return {
                            "api_key": route.api_key,
                            "base_url": route.base_url.rstrip("/"),
                            "model": model,
                            "dimensions": _get_embedding_config()["dimensions"],
                            "provider": route.provider,
                            "auth_method": (
                                "oidc"
                                if str(route.source_detail or "").strip()
                                == "VERCEL_OIDC_TOKEN"
                                else "api-key"
                            ),
                        }
                return None
            return {
                "api_key": metadata["llm_api_key"],
                "base_url": str(metadata.get("llm_base_url") or "https://api.openai.com/v1").rstrip("/"),
                "model": model or "text-embedding-3-small",
                "dimensions": _get_embedding_config()["dimensions"],
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

    current_text = text_input
    for attempt in range(3):
        try:
            embeddings, usage = await _post_embeddings(
                cfg,
                [current_text],
                model=model or cfg["model"],
                timeout=60.0,
            )
        except Exception as exc:
            message = str(exc)
            if "context length" in message.lower() or "too long" in message.lower():
                current_text = current_text[: len(current_text) // 2]
                logger.warning(
                    "Embedding input too long, truncating to %d chars (attempt %d)",
                    len(current_text),
                    attempt + 1,
                )
                continue
            raise
        if not embeddings:
            raise RuntimeError("Embedding provider returned no vectors")
        embedding = embeddings[0]

        # Bill the call when a billing context is set + provider is paid.
        await _bill_embedding(
            cfg=cfg,
            model_used=model or cfg["model"],
            usage=usage,
        )
        return embedding

    raise RuntimeError(f"Embedding failed after truncation retries for text of length {len(text_input)}")


async def generate_embeddings_batch(
    texts: list[str],
    *,
    model: str | None = None,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
    cache_namespace: str | None = None,
) -> list[list[float]]:
    """Batch embedding generation.

    For providers with small context windows (e.g. Ollama), falls back to
    processing texts one-by-one via generate_embedding() which handles
    truncation retries.
    """
    cfg = await _resolve_embedding_config()
    if not cfg:
        raise RuntimeError("No embedding provider available")

    resolved_model = str(model or cfg["model"])
    expected_dimensions = int(cfg.get("dimensions") or 0)

    def _cache_key(value: str) -> str:
        identity = json.dumps(
            {
                "namespace": cache_namespace or "shared",
                "base_url": cfg["base_url"],
                "model": resolved_model,
                "dimensions": expected_dimensions,
                "text": value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"embedding:v1:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"

    def _cached_vector(value: object) -> list[float] | None:
        if not isinstance(value, list) or expected_dimensions <= 0:
            return None
        if len(value) != expected_dimensions:
            return None
        if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
            return None
        return [float(item) for item in value]

    cache_keys = [_cache_key(value) for value in texts]
    cached_values = (
        await cache.get_many(cache_keys)
        if EMBEDDING_CACHE_TTL_SECONDS > 0
        else [None] * len(texts)
    )
    results: list[list[float] | None] = [
        _cached_vector(value) for value in cached_values
    ]
    completed = sum(result is not None for result in results)
    if completed and on_progress:
        await on_progress(completed)

    missing_indices = [index for index, result in enumerate(results) if result is None]
    if not missing_indices:
        return [result for result in results if result is not None]

    is_ollama = not cfg["api_key"] or cfg["api_key"] == "ollama"

    # Ollama doesn't reliably support batch embedding — process individually
    if is_ollama:
        for index in missing_indices:
            embedding = await generate_embedding(texts[index], model=model)
            results[index] = embedding
            if EMBEDDING_CACHE_TTL_SECONDS > 0 and _cached_vector(embedding) is not None:
                await cache.set_many(
                    {cache_keys[index]: embedding},
                    ttl=EMBEDDING_CACHE_TTL_SECONDS,
                )
            completed += 1
            if on_progress:
                await on_progress(completed)
        return [result for result in results if result is not None]

    batch_size = 100

    for i in range(0, len(missing_indices), batch_size):
        batch_indices = missing_indices[i : i + batch_size]
        batch = [texts[index] for index in batch_indices]
        embeddings, usage = await _post_embeddings(
            cfg,
            batch,
            model=resolved_model,
            timeout=120.0,
        )
        if len(embeddings) != len(batch_indices):
            raise RuntimeError(
                f"Embedding provider returned {len(embeddings)} vectors for {len(batch_indices)} inputs"
            )
        cache_updates: dict[str, list[float]] = {}
        for document_index, embedding in zip(batch_indices, embeddings, strict=True):
            results[document_index] = embedding
            if _cached_vector(embedding) is not None:
                cache_updates[cache_keys[document_index]] = embedding
        if EMBEDDING_CACHE_TTL_SECONDS > 0:
            await cache.set_many(cache_updates, ttl=EMBEDDING_CACHE_TTL_SECONDS)
        completed += len(embeddings)
        if on_progress:
            await on_progress(completed)
        # Bill this batch — usage.total_tokens is summed across the batch.
        await _bill_embedding(
            cfg=cfg,
            model_used=resolved_model,
            usage=usage,
        )

    return [result for result in results if result is not None]


async def _post_embeddings(
    cfg: dict,
    values: list[str],
    *,
    model: str,
    timeout: float,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Call either the Vercel v4 or OpenAI-compatible embedding protocol."""

    expected_dimensions = int(cfg.get("dimensions") or 0)
    if expected_dimensions < 1:
        raise RuntimeError("Embedding dimensions must be a positive integer")

    if cfg.get("provider") == "vercel":
        from packages.core.services.vercel_ai_gateway import vercel_gateway_post

        data = await vercel_gateway_post(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=model,
            protocol="embedding",
            payload={
                "values": values,
                # AI Gateway forwards this to OpenAI's embedding adapter.
                # Keep the gateway route on the same pgvector dimension as
                # local, BYOK, and environment-configured routes.
                "providerOptions": {
                    "openai": {"dimensions": expected_dimensions},
                },
            },
            auth_method=cfg.get("auth_method") or "api-key",
            timeout=timeout,
        )
        embeddings = data.get("embeddings") or []
        usage = data.get("usage") or {}
        if isinstance(usage, dict) and "tokens" in usage:
            usage = {**usage, "total_tokens": usage.get("tokens")}
        return _validate_embedding_dimensions(embeddings, expected_dimensions), usage

    url = f"{cfg['base_url']}/embeddings"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {"input": values, "model": model}
    if cfg.get("api_key") != "ollama":
        payload["dimensions"] = expected_dimensions
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    items = sorted(data["data"], key=lambda item: item["index"])
    embeddings = [item["embedding"] for item in items]
    return _validate_embedding_dimensions(embeddings, expected_dimensions), data.get("usage") or {}


def _validate_embedding_dimensions(
    embeddings: object,
    expected_dimensions: int,
) -> list[list[float]]:
    """Reject a provider response that cannot fit this deployment's pgvector schema."""

    if not isinstance(embeddings, list):
        raise RuntimeError("Embedding provider returned an invalid vector payload")

    validated: list[list[float]] = []
    for index, embedding in enumerate(embeddings):
        if not isinstance(embedding, list):
            raise RuntimeError(f"Embedding provider returned an invalid vector at index {index}")
        actual_dimensions = len(embedding)
        if actual_dimensions != expected_dimensions:
            raise RuntimeError(
                "Embedding provider returned "
                f"{actual_dimensions} dimensions; expected {expected_dimensions} dimensions"
            )
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in embedding):
            raise RuntimeError(f"Embedding provider returned a non-numeric vector at index {index}")
        validated.append([float(value) for value in embedding])
    return validated


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
        from packages.core.services.entity_fs import (
            entity_filesystem_read_lock,
            get_entity_root,
        )

        snapshots: list[str] = []
        candidates: list[str] = []
        try:
            # Keep the shared lock only long enough to capture stable bytes.
            # Format parsing can be slow and must not block trash, restore, or edits.
            async with entity_filesystem_read_lock(get_entity_root(doc.entity_id)):
                candidates = _document_fs_candidates(doc)
                for path in candidates:
                    if not os.path.isfile(path):
                        continue
                    fd, snapshot_path = tempfile.mkstemp(suffix=os.path.splitext(path)[1])
                    os.close(fd)
                    try:
                        await asyncio.to_thread(shutil.copyfile, path, snapshot_path)
                    except BaseException:
                        await asyncio.to_thread(os.unlink, snapshot_path)
                        raise
                    snapshots.append(snapshot_path)

                if not snapshots:
                    raise DocumentContentUnavailable(
                        status="missing",
                        fs_path=str(doc.fs_path),
                        path=candidates[0] if candidates else None,
                    )

            for snapshot_path in snapshots:
                content = await extract_text(
                    snapshot_path,
                    mime_type=doc.mime_type,
                    file_type=doc.file_type,
                )
                if content:
                    return content
        finally:
            for snapshot_path in snapshots:
                await asyncio.to_thread(os.unlink, snapshot_path)

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
    *,
    run_id: str | None = None,
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

    metadata = _with_file_integrity(meta, **fields)
    status = VectorStatus.PENDING if exc.status == "unavailable" else VectorStatus.SKIPPED
    if run_id is None:
        doc.metadata_ = metadata
        doc.vector_status = status
        await db.commit()
        return

    updated_at = datetime.now(timezone.utc)
    result = await db.execute(
        text(
            """
            UPDATE documents
               SET metadata = CAST(:metadata AS jsonb),
                   vector_status = :status,
                   updated_at = :updated_at
             WHERE id = :doc_id
               AND is_trashed = false
               AND vector_status = :processing
               AND metadata->'indexing'->>'run_id' = :run_id
            """
        ),
        {
            "doc_id": doc.id,
            "run_id": run_id,
            "processing": VectorStatus.PROCESSING,
            "status": status,
            "metadata": json.dumps(metadata),
            "updated_at": updated_at,
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _IndexingClaimLost(doc.id)
    await db.commit()


def _chunk_text(text_content: str, max_chars: int = _RETRIEVAL_CHUNK_CHARS) -> list[str]:
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


# ---------------------------------------------------------------------------
# Document indexing
# ---------------------------------------------------------------------------


async def _claim_document_indexing(
    db: AsyncSession,
    doc: Document,
    *,
    allow_ready: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Atomically claim a document for one indexing run.

    Duplicate deliveries see the fresh ``processing`` heartbeat and become a
    no-op. A redelivery may reclaim a processing row only after its heartbeat
    is stale. Ready/skipped documents are never silently re-indexed.
    """

    claimable_statuses = {
        VectorStatus.PENDING,
        VectorStatus.FAILED,
        VectorStatus.PROCESSING,
    }
    if allow_ready:
        claimable_statuses.add(VectorStatus.READY)
        claimable_statuses.add(VectorStatus.INDEXED)
    if doc.vector_status not in claimable_statuses:
        return None

    claimed_at = now or datetime.now(timezone.utc)
    stale_before = claimed_at - timedelta(seconds=EMBEDDING_PROCESSING_STALE_SECONDS)
    previous_indexing = (
        dict((doc.metadata_ or {}).get("indexing") or {})
        if isinstance(doc.metadata_, dict)
        else {}
    )
    run_id = generate_ulid()
    indexing = {
        "run_id": run_id,
        "step": "starting",
        "progress": 0,
        "total_chunks": 0,
        "current_chunk": 0,
        "heartbeat_at": claimed_at.isoformat(),
        "heartbeat_epoch": claimed_at.timestamp(),
        "started_at": claimed_at.isoformat(),
        "recovery_attempts": int(previous_indexing.get("recovery_attempts") or 0),
    }
    result = await db.execute(
        text(
            """
            UPDATE documents
               SET vector_status = :processing,
                   metadata = jsonb_set(
                       COALESCE(metadata, '{}'::jsonb),
                       '{indexing}',
                       CAST(:indexing AS jsonb),
                       true
                   )
             WHERE id = :doc_id
               AND is_trashed = false
               AND (
                    vector_status IN (:pending, :failed)
                    OR (:allow_ready AND vector_status IN (:ready, :indexed))
                    OR (
                        vector_status = :processing
                        AND COALESCE(
                            CASE
                                WHEN jsonb_typeof(metadata->'indexing'->'heartbeat_epoch') = 'number'
                                THEN to_timestamp((metadata->'indexing'->>'heartbeat_epoch')::double precision)
                            END,
                            updated_at,
                            created_at
                        ) < :stale_before
                    )
               )
         RETURNING id
            """
        ),
        {
            "doc_id": doc.id,
            "pending": VectorStatus.PENDING,
            "failed": VectorStatus.FAILED,
            "ready": VectorStatus.READY,
            "indexed": VectorStatus.INDEXED,
            "allow_ready": allow_ready,
            "processing": VectorStatus.PROCESSING,
            "stale_before": stale_before,
            "indexing": json.dumps(indexing),
        },
    )
    claimed = result.scalar_one_or_none()
    await db.commit()
    if not claimed:
        return None
    await db.refresh(doc)
    return run_id


async def _update_document_indexing_progress(
    db: AsyncSession,
    document_id: str,
    run_id: str,
    *,
    step: str,
    progress: int,
    total_chunks: int = 0,
    current_chunk: int = 0,
) -> None:
    """Write a heartbeat only while this run still owns the document."""

    heartbeat_at = datetime.now(timezone.utc)
    progress_data = {
        "run_id": run_id,
        "step": step,
        "progress": max(0, min(100, int(progress))),
        "total_chunks": max(0, int(total_chunks)),
        "current_chunk": max(0, int(current_chunk)),
        "heartbeat_at": heartbeat_at.isoformat(),
        "heartbeat_epoch": heartbeat_at.timestamp(),
    }
    result = await db.execute(
        text(
            """
            UPDATE documents
               SET metadata = jsonb_set(
                       COALESCE(metadata, '{}'::jsonb),
                       '{indexing}',
                       COALESCE(metadata->'indexing', '{}'::jsonb) || CAST(:progress_data AS jsonb),
                       true
                   )
             WHERE id = :doc_id
               AND is_trashed = false
               AND vector_status = :processing
               AND metadata->'indexing'->>'run_id' = :run_id
            """
        ),
        {
            "doc_id": document_id,
            "run_id": run_id,
            "processing": VectorStatus.PROCESSING,
            "progress_data": json.dumps(progress_data),
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _IndexingClaimLost(document_id)
    await db.commit()


async def _complete_document_indexing_without_vectors(
    db: AsyncSession,
    document_id: str,
    run_id: str,
) -> None:
    completed_at = datetime.now(timezone.utc)
    result = await db.execute(
        text(
            """
            UPDATE documents
               SET vector_status = :ready,
                   metadata = COALESCE(metadata, '{}'::jsonb) - 'indexing',
                   updated_at = :completed_at
             WHERE id = :doc_id
               AND is_trashed = false
               AND vector_status = :processing
               AND metadata->'indexing'->>'run_id' = :run_id
            """
        ),
        {
            "doc_id": document_id,
            "run_id": run_id,
            "processing": VectorStatus.PROCESSING,
            "ready": VectorStatus.READY,
            "completed_at": completed_at,
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _IndexingClaimLost(document_id)
    await db.commit()


async def _defer_document_indexing_for_deleted_workspace(
    db: AsyncSession,
    document: Document,
    *,
    run_id: str | None = None,
) -> None:
    """Leave deleted-Workspace indexing pending for a later restore."""
    deferred_at = datetime.now(timezone.utc)
    blocked_data = {
        "step": "blocked",
        "progress": 0,
        "blocked_at": deferred_at.isoformat(),
        "blocked_reason": "workspace_deleted",
    }
    if run_id is None:
        if document.vector_status not in {
            VectorStatus.PENDING,
            VectorStatus.PROCESSING,
            VectorStatus.FAILED,
        }:
            return
        metadata = dict(document.metadata_ or {})
        indexing = dict(metadata.get("indexing") or {})
        indexing.update(blocked_data)
        indexing.pop("run_id", None)
        indexing.pop("heartbeat_at", None)
        indexing.pop("heartbeat_epoch", None)
        metadata["indexing"] = indexing
        document.metadata_ = metadata
        document.vector_status = VectorStatus.PENDING
        await db.commit()
        return

    result = await db.execute(
        text(
            """
            UPDATE documents
               SET vector_status = :pending,
                   metadata = jsonb_set(
                       COALESCE(metadata, '{}'::jsonb),
                       '{indexing}',
                       (COALESCE(metadata->'indexing', '{}'::jsonb) - 'run_id'
                           - 'heartbeat_at' - 'heartbeat_epoch')
                           || CAST(:blocked_data AS jsonb),
                       true
                   ),
                   updated_at = :deferred_at
             WHERE id = :doc_id
               AND is_trashed = false
               AND vector_status = :processing
               AND metadata->'indexing'->>'run_id' = :run_id
            """
        ),
        {
            "doc_id": document.id,
            "run_id": run_id,
            "pending": VectorStatus.PENDING,
            "processing": VectorStatus.PROCESSING,
            "blocked_data": json.dumps(blocked_data),
            "deferred_at": deferred_at,
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _IndexingClaimLost(document.id)
    await db.commit()


async def _defer_indexing_if_workspace_deleted(
    db: AsyncSession,
    document: Document,
    *,
    run_id: str | None = None,
    lock_for_read: bool = False,
) -> bool:
    from packages.core.services.document_access import (
        document_is_owned_by_deleted_workspace,
    )

    await db.refresh(document)
    if not await document_is_owned_by_deleted_workspace(
        db,
        document,
        lock_for_read=lock_for_read,
    ):
        return False
    await _defer_document_indexing_for_deleted_workspace(
        db,
        document,
        run_id=run_id,
    )
    return True


async def _mark_document_indexing_failed(
    db: AsyncSession,
    document_id: str,
    run_id: str,
    error: BaseException,
) -> bool:
    failed_at = datetime.now(timezone.utc)
    failure_data = {
        "run_id": run_id,
        "step": "failed",
        "progress": 0,
        "heartbeat_at": failed_at.isoformat(),
        "failed_at": failed_at.isoformat(),
        "error": str(error)[:500] or error.__class__.__name__,
    }
    result = await db.execute(
        text(
            """
            UPDATE documents
               SET vector_status = :failed,
                   metadata = jsonb_set(
                       COALESCE(metadata, '{}'::jsonb),
                       '{indexing}',
                       COALESCE(metadata->'indexing', '{}'::jsonb) || CAST(:failure_data AS jsonb),
                       true
                   ),
                   updated_at = :failed_at
             WHERE id = :doc_id
               AND is_trashed = false
               AND vector_status = :processing
               AND metadata->'indexing'->>'run_id' = :run_id
            """
        ),
        {
            "doc_id": document_id,
            "run_id": run_id,
            "processing": VectorStatus.PROCESSING,
            "failed": VectorStatus.FAILED,
            "failed_at": failed_at,
            "failure_data": json.dumps(failure_data),
        },
    )
    await db.commit()
    return result.rowcount == 1


async def index_document(
    db: AsyncSession,
    document_id: str,
    *,
    allow_ready: bool = True,
) -> bool:
    """Generate embedding for a document and store it.

    1. Load document from DB
    2. Read document content
    3. Chunk if needed and generate one embedding per retrieval chunk
    4. Store chunks + set vector_status = 'ready'

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
    if await _defer_indexing_if_workspace_deleted(db, doc):
        logger.info("Deferring embedding for deleted-Workspace document %s", document_id)
        return True

    run_id = await _claim_document_indexing(db, doc, allow_ready=allow_ready)
    if not run_id:
        logger.info(
            "Skipping duplicate or obsolete embedding delivery for document %s (status=%s)",
            document_id,
            doc.vector_status,
        )
        return True

    async def _update_progress(
        step: str,
        progress: int,
        total_chunks: int = 0,
        current_chunk: int = 0,
    ) -> None:
        await _update_document_indexing_progress(
            db,
            document_id,
            run_id,
            step=step,
            progress=progress,
            total_chunks=total_chunks,
            current_chunk=current_chunk,
        )

    try:
        if await _defer_indexing_if_workspace_deleted(db, doc, run_id=run_id):
            logger.info("Deferring embedding for deleted-Workspace document %s", document_id)
            return True
        # File availability is checked only after this delivery owns the run.
        # Otherwise a duplicate delivery could cancel a healthy active worker.
        _ensure_document_file_available(doc)

        # Skip embedding if no provider is available. Text and trigram search
        # still work, so this is a successful terminal state.
        cfg = await _resolve_embedding_config()
        if not cfg:
            if await _defer_indexing_if_workspace_deleted(
                db,
                doc,
                run_id=run_id,
                lock_for_read=True,
            ):
                return True
            logger.info("No embedding provider available — skipping vectorization for document %s", document_id)
            await _complete_document_indexing_without_vectors(db, document_id, run_id)
            return True

        await _update_progress("reading", 10)
        if await _defer_indexing_if_workspace_deleted(
            db,
            doc,
            run_id=run_id,
            lock_for_read=True,
        ):
            return True
        # The shared Workspace lifecycle lock only protects the ownership
        # decision.  Release it before filesystem I/O so delete/restore is not
        # blocked by a slow remote object-store read.
        await db.commit()
        content = await _read_document_content(doc)
        if await _defer_indexing_if_workspace_deleted(db, doc, run_id=run_id):
            return True

        await _update_progress("chunking", 20)
        # Use smaller chunks for Ollama (limited context window)
        chunk_size = _OLLAMA_CHUNK_CHARS if not cfg["api_key"] or cfg["api_key"] == "ollama" else _RETRIEVAL_CHUNK_CHARS
        chunks = _chunk_text(content, max_chars=chunk_size)
        total = len(chunks)

        await _update_progress("embedding", 30, total_chunks=total, current_chunk=0)

        # Each chunk gets its OWN embedding — no averaging. Averaging collapsed
        # a whole document into one point, so a fact three pages in got no more
        # representation in the vector than the title did; independent chunk
        # embeddings let retrieval find the paragraph, not just the document.
        batch_size = 100
        chunk_embeddings: list[list[float]] = []
        for i in range(0, total, batch_size):
            if await _defer_indexing_if_workspace_deleted(
                db,
                doc,
                run_id=run_id,
                lock_for_read=True,
            ):
                return True
            # Never carry a Workspace lifecycle lock into provider I/O.  The
            # final storing check below reacquires it for the atomic write.
            await db.commit()
            batch = chunks[i : i + batch_size]
            reported_done = i

            async def _heartbeat(batch_done: int) -> None:
                nonlocal reported_done
                reported_done = min(i + batch_done, total)
                pct = 30 + int(60 * reported_done / total)
                await _update_progress(
                    "embedding",
                    pct,
                    total_chunks=total,
                    current_chunk=reported_done,
                )
                if await _defer_indexing_if_workspace_deleted(
                    db,
                    doc,
                    run_id=run_id,
                    lock_for_read=True,
                ):
                    raise _IndexingClaimLost(document_id)
                await db.commit()

            batch_embeddings = await generate_embeddings_batch(
                batch,
                on_progress=_heartbeat,
                cache_namespace=doc.entity_id,
            )
            if len(batch_embeddings) != len(batch):
                # A provider returning fewer/more embeddings than inputs sent
                # cannot be safely zipped to chunks — silently pairing them
                # positionally would attach the wrong vector to the wrong
                # text. Treat it as a hard failure rather than guess.
                raise RuntimeError(
                    f"embedding provider returned {len(batch_embeddings)} vectors "
                    f"for {len(batch)} inputs"
                )
            chunk_embeddings.extend(batch_embeddings)
            done = min(i + batch_size, total)
            if reported_done < done:
                pct = 30 + int(60 * done / total)
                await _update_progress("embedding", pct, total_chunks=total, current_chunk=done)

        await _update_progress("storing", 95, total_chunks=total, current_chunk=total)
        if await _defer_indexing_if_workspace_deleted(
            db,
            doc,
            run_id=run_id,
            lock_for_read=True,
        ):
            return True

        conn = await db.connection()
        # Replace this document's chunks wholesale — reindexing must not
        # accumulate stale rows alongside fresh ones.
        await conn.execute(
            text("DELETE FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": document_id},
        )
        for index, (chunk_content, embedding) in enumerate(zip(chunks, chunk_embeddings)):
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await conn.execute(
                text(
                    "INSERT INTO document_chunks (id, document_id, chunk_index, content, embedding) "
                    "VALUES (:id, :doc_id, :idx, :content, CAST(:vec AS vector))"
                ),
                {
                    "id": generate_ulid(),
                    "doc_id": document_id,
                    "idx": index,
                    "content": chunk_content,
                    "vec": vec_str,
                },
            )

        # documents.vector_status stays the one signal for "is this document
        # indexed" — document_chunks now hold the actual embeddings.
        completed_at = datetime.now(timezone.utc)
        ready_result = await conn.execute(
            text(
                """
                UPDATE documents
                   SET vector_status = :ready,
                       metadata = COALESCE(metadata, '{}'::jsonb) - 'indexing',
                       updated_at = :completed_at
                 WHERE id = :doc_id
                   AND is_trashed = false
                   AND vector_status = :processing
                   AND metadata->'indexing'->>'run_id' = :run_id
                """
            ),
            {
                "ready": VectorStatus.READY,
                "processing": VectorStatus.PROCESSING,
                "doc_id": document_id,
                "run_id": run_id,
                "completed_at": completed_at,
            },
        )
        if ready_result.rowcount != 1:
            raise _IndexingClaimLost(document_id)
        await db.commit()

        logger.info("Indexed document %s (%d chunks)", document_id, len(chunks))
        return True

    except DocumentContentUnavailable as exc:
        logger.info("Skipping embedding for document %s: %s", document_id, exc)
        try:
            await db.rollback()
            # rollback expires ORM attributes; refresh the current metadata,
            # then let the conditional run_id update resolve any race.
            await db.refresh(doc)
            await _mark_document_content_unavailable(db, doc, exc, run_id=run_id)
        except _IndexingClaimLost:
            logger.info(
                "Indexing ownership lost while recording unavailable content for document %s",
                document_id,
            )
            return True
        except Exception:
            logger.exception("Failed to record unavailable content for document %s", document_id)
        return False

    except _IndexingClaimLost:
        await db.rollback()
        logger.info("Indexing ownership lost for document %s; stopping stale run %s", document_id, run_id)
        return True

    except Exception as exc:
        logger.exception("Failed to index document %s", document_id)
        try:
            await db.rollback()
            await _mark_document_indexing_failed(db, document_id, run_id, exc)
        except Exception:
            # Session is broken — use a fresh engine to update status
            logger.warning("Session broken for %s, using fresh connection to mark failed", document_id)
            try:
                from packages.core.database import create_worker_session
                fresh_factory = create_worker_session()
                async with fresh_factory() as fresh_db:
                    await _mark_document_indexing_failed(fresh_db, document_id, run_id, exc)
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


async def search_similar_chunks(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 20,
    threshold: float = 0.5,
    allowed_doc_ids: set[str] | None = None,
) -> list[dict]:
    """Chunk-level semantic search using pgvector cosine similarity.

    Returns one row per matching CHUNK, ranked by similarity — a document can
    appear more than once; callers wanting one hit per document should dedupe
    keeping the first (highest-scored) occurrence, since rows are pre-ordered.
    """
    cfg = await _resolve_embedding_config()
    if not cfg:
        return []
    query_embedding = await generate_embedding(query)
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    conditions = [
        "d.entity_id = :eid",
        "d.is_trashed = false",
        "dc.embedding IS NOT NULL",
        "1 - (dc.embedding <=> CAST(:query_vec AS vector)) >= :threshold",
    ]
    params: dict[str, object] = {
        "query_vec": vec_str, "eid": entity_id, "threshold": threshold, "lim": limit,
    }
    if allowed_doc_ids is not None:
        if not allowed_doc_ids:
            return []
        conditions.append("dc.document_id = ANY(:doc_ids)")
        params["doc_ids"] = list(allowed_doc_ids)

    # ivfflat is an approximate index: with the default probes=1 it scans only
    # the single nearest cluster of its 100 lists, and at low chunk counts
    # (a new or small workspace, or any table before autovacuum has run
    # ANALYZE) a real match can land in a cluster that never gets probed and
    # is silently missed — not a slow query, a WRONG one. SET LOCAL scopes
    # this to the current transaction only, so it can't leak the setting onto
    # other queries sharing a pooled connection.
    await db.execute(text("SET LOCAL ivfflat.probes = 10"))
    # Only fixed predicates above are interpolated; every value remains bound.
    statement = (
        "SELECT dc.document_id, dc.content, d.name, "
        "1 - (dc.embedding <=> CAST(:query_vec AS vector)) AS score "
        "FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
        f"WHERE {' AND '.join(conditions)} "  # nosec B608
        "ORDER BY dc.embedding <=> CAST(:query_vec AS vector) LIMIT :lim"
    )
    sql = text(statement)
    rows = (await db.execute(sql, params)).fetchall()
    return [
        {
            "document_id": row.document_id,
            "name": row.name,
            "score": round(float(row.score), 4),
            "content_preview": row.content[:_PREVIEW_MAX_CHARS],
        }
        for row in rows
    ]


async def search_similar_trigram(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 20,
    allowed_doc_ids: set[str] | None = None,
) -> list[dict]:
    """Chunk-level lexical search via Postgres ``pg_trgm``.

    Character-trigram similarity rather than word tokenization: it scales via
    the GIN index to the whole corpus (the fallback this replaced scanned at
    most 25 documents, unindexed, per query), and it works on CJK text, which
    Postgres's built-in text-search parsers do not segment into words at all.
    No embedding provider required — this path is always available.
    """
    conditions = ["d.entity_id = :eid", "d.is_trashed = false"]
    params: dict[str, object] = {"query": query, "eid": entity_id, "lim": limit}
    if allowed_doc_ids is not None:
        if not allowed_doc_ids:
            return []
        conditions.append("dc.document_id = ANY(:doc_ids)")
        params["doc_ids"] = list(allowed_doc_ids)

    # Only fixed predicates above are interpolated; every value remains bound.
    statement = (
        "SELECT dc.document_id, dc.content, d.name, "
        "similarity(dc.content, :query) AS score "
        "FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
        f"WHERE {' AND '.join(conditions)} "  # nosec B608
        "ORDER BY similarity(dc.content, :query) DESC LIMIT :lim"
    )
    sql = text(statement)
    rows = (await db.execute(sql, params)).fetchall()
    return [
        {
            "document_id": row.document_id,
            "name": row.name,
            "score": round(float(row.score), 4),
            "content_preview": row.content[:_PREVIEW_MAX_CHARS],
        }
        for row in rows
        if row.score and row.score > 0
    ]


def _dedupe_best_per_document(ranked_chunk_hits: list[dict]) -> list[dict]:
    """Collapse a chunk-ranked list to one row per document (its best chunk),
    preserving rank order — fusion combines per-DOCUMENT ranks, and the final
    result contract is one row per document, matching every caller from
    before chunking existed."""
    seen: set[str] = set()
    out = []
    for hit in ranked_chunk_hits:
        doc_id = hit["document_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(hit)
    return out


def _reciprocal_rank_fusion(ranked_lists: list[list[dict]], *, k: int = 60) -> dict[str, float]:
    """Fuse several document-ranked lists into one score per document_id,
    using each item's RANK in each list rather than its raw score.

    Vector cosine similarity, trigram similarity, and a hand-tuned 0.9/0.55
    filename-match score live on incompatible scales — adding them lets
    whichever source happens to produce bigger numbers dominate regardless of
    actual relevance. Reciprocal Rank Fusion sidesteps the scale problem
    entirely by ignoring the scores and only looking at position; it is the
    default hybrid-search fusion in Elasticsearch, OpenSearch, and Azure AI
    Search for the same reason. ``k=60`` is the value all three of those use.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            doc_id = item["document_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


async def _unindexed_scope_results(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int,
    allowed_doc_ids: set[str],
) -> list[dict]:
    """Read-the-file fallback for documents that have no chunks yet.

    document_chunks only exist once index_document has run — but a Knowledge
    Net or workspace scope is a small, explicit membership list the caller
    picked (``the set is already constrained by membership`` — this is why
    the read-on-the-fly cost here is acceptable when it would not be for an
    entity-wide search), and a freshly-uploaded or never-embedded file must
    still be searchable within its own scope. Entity-wide (unscoped) search
    does not get this path — see search_similar_trigram for that, which is
    indexed and does not require reading every file on every query.
    """
    terms = _lexical_terms(query)
    if not terms or not allowed_doc_ids:
        return []

    from packages.core.models.document import Document

    docs = list((await db.execute(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
            Document.id.in_(allowed_doc_ids),
        )
    )).scalars().all())

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
        # Explicit scope selection is itself evidence the caller wants this
        # corpus considered — a low-score preview beats a false empty result.
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
    lowered = query.lower()
    raw = re.findall(r"[a-z0-9_\-一-鿿]+", lowered)
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


async def hybrid_search(
    db: AsyncSession,
    entity_id: str,
    query: str,
    *,
    limit: int = 10,
    workspace_id: str | None = None,
    group_ids: list[str] | None = None,
) -> list[dict]:
    """Hybrid search: chunk-level vector + chunk-level trigram-lexical +
    filename match (+ a scoped read-the-file fallback), fused by Reciprocal
    Rank Fusion.

    1. Vector search over document_chunks (semantic) — finds the paragraph
       that answers the question, not just the document it lives in.
    2. Trigram search over document_chunks (lexical, CJK-safe, indexed,
       covers the whole corpus regardless of size).
    3. Filename match — the user sometimes remembers the file, not its text.
    4. When the caller passed workspace_id/group_ids: also read any
       still-unindexed document's file directly (see
       _unindexed_scope_results). document_chunks only exist once
       index_document has actually run; a freshly-uploaded file in an
       explicitly scoped Knowledge Net must still be searchable before that
       happens. Entity-wide search skips this — it is not indexed or bounded,
       and would mean reading every file in the entity on every query.
    5. Fuse by rank (see _reciprocal_rank_fusion for why not by score), then
       normalize the fused score to the top hit in THIS result set so the
       number shown to callers (e.g. "relevance: 0.87" in the rag tool's
       output) reads as a confidence, not a tiny raw RRF sum.
    """
    allowed_doc_ids = (
        await _group_document_ids(db, entity_id, group_ids)
        if group_ids
        else await _workspace_document_ids(db, entity_id, workspace_id) if workspace_id else None
    )
    candidate_limit = max(limit * 4, 20)

    vector_hits: list[dict] = []
    if await _resolve_embedding_config():
        try:
            # Keep provider/schema mismatches (for example a stale 1536-dim
            # row against the local 1024-dim pgvector column) inside a
            # savepoint. PostgreSQL aborts the current transaction on that
            # error; without the nested rollback the lexical and filesystem
            # fallbacks, followed by the worker's failure write, all fail with
            # ``InFailedSQLTransactionError`` and leave the lease active.
            async with db.begin_nested():
                vector_hits = await search_similar_chunks(
                    db, entity_id, query, limit=candidate_limit, threshold=0.5,
                    allowed_doc_ids=allowed_doc_ids,
                )
        except Exception:
            logger.warning("Vector search failed, falling back to lexical only", exc_info=True)

    try:
        trigram_hits = await search_similar_trigram(
            db, entity_id, query, limit=candidate_limit, allowed_doc_ids=allowed_doc_ids,
        )
    except Exception:
        logger.warning("Trigram search failed", exc_info=True)
        trigram_hits = []

    unindexed_hits: list[dict] = []
    if allowed_doc_ids is not None:
        try:
            unindexed_hits = await _unindexed_scope_results(
                db, entity_id, query, limit=candidate_limit, allowed_doc_ids=allowed_doc_ids,
            )
        except Exception:
            logger.warning("Unindexed-scope fallback failed", exc_info=True)

    from packages.core.services.document_service import list_documents

    text_docs, _ = await list_documents(
        db, entity_id, name_search=query, limit=candidate_limit,
    )
    query_lower = query.lower()
    filename_hits: list[dict] = []
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
        filename_hits.append({
            "document_id": doc.id,
            "name": doc.name,
            "score": 0.9 if name_substring_hit else 0.55,
            "content_preview": preview,
        })
    # ILIKE gives no natural rank beyond exact-vs-fuzzy; put exact substring
    # hits first so rank-based fusion treats them as stronger than the fuzzy
    # metadata matches list_documents(name_search=...) also returns.
    filename_hits.sort(key=lambda r: r["score"], reverse=True)

    vector_by_doc = _dedupe_best_per_document(vector_hits)
    trigram_by_doc = _dedupe_best_per_document(trigram_hits)
    unindexed_by_doc = _dedupe_best_per_document(unindexed_hits)

    fused_scores = _reciprocal_rank_fusion(
        [vector_by_doc, trigram_by_doc, unindexed_by_doc, filename_hits]
    )
    if not fused_scores:
        return []

    # Prefer a real chunk's own text as the preview (vector, then trigram),
    # then the read-on-the-fly scope fallback, then plain filename match —
    # each step down is progressively less direct evidence of an actual
    # content match, and content_preview == name for a pure filename hit is
    # what lets callers (see _runtime_rag_result_has_content) tell a real
    # content match from "we only matched the filename".
    by_doc: dict[str, dict] = {}
    for hit in filename_hits:
        by_doc[hit["document_id"]] = hit
    for hit in unindexed_by_doc:
        by_doc[hit["document_id"]] = hit
    for hit in trigram_by_doc:
        by_doc[hit["document_id"]] = hit
    for hit in vector_by_doc:
        by_doc[hit["document_id"]] = hit

    max_score = max(fused_scores.values())
    ranked_ids = sorted(fused_scores, key=lambda doc_id: fused_scores[doc_id], reverse=True)
    results = []
    for doc_id in ranked_ids[:limit]:
        row = dict(by_doc[doc_id])
        row["score"] = round(fused_scores[doc_id] / max_score, 4)
        results.append(row)
    return results



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
