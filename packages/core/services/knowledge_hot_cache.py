"""Bounded Redis hot cache for on-demand Knowledge payloads.

Knowledge listings remain metadata-only. The first authorized open renders or
reads the payload from durable storage; later opens can use Redis. Cache keys
include the document version, so writes make old entries unreachable without a
global delete, and every hit extends the TTL so frequently used files stay hot.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any

from packages.core.cache import cache


def _nonnegative_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


KNOWLEDGE_CONTENT_CACHE_TTL_SECONDS = _nonnegative_int_env(
    "KNOWLEDGE_CONTENT_CACHE_TTL_SECONDS",
    172_800,
)
KNOWLEDGE_THUMBNAIL_CACHE_TTL_SECONDS = _nonnegative_int_env(
    "KNOWLEDGE_THUMBNAIL_CACHE_TTL_SECONDS",
    172_800,
)
KNOWLEDGE_DOWNLOAD_CACHE_TTL_SECONDS = _nonnegative_int_env(
    "KNOWLEDGE_DOWNLOAD_CACHE_TTL_SECONDS",
    172_800,
)
KNOWLEDGE_CACHE_MAX_ENTRY_BYTES = _nonnegative_int_env(
    "KNOWLEDGE_CACHE_MAX_ENTRY_BYTES",
    1_048_576,
)

_BLOB_TTLS = {
    "download": KNOWLEDGE_DOWNLOAD_CACHE_TTL_SECONDS,
    "thumbnail": KNOWLEDGE_THUMBNAIL_CACHE_TTL_SECONDS,
}


@dataclass(frozen=True)
class CachedKnowledgeBlob:
    data: bytes
    media_type: str


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def document_hot_cache_key(document: Any, kind: str) -> str:
    """Return a content-versioned key without exposing paths in Redis."""
    identity = json.dumps(
        {
            "kind": kind,
            "entity_id": str(getattr(document, "entity_id", "") or ""),
            "document_id": str(getattr(document, "id", "") or ""),
            "updated_at": _timestamp(getattr(document, "updated_at", None)),
            "created_at": _timestamp(getattr(document, "created_at", None)),
            "file_size": int(getattr(document, "file_size", 0) or 0),
            "fs_path": str(getattr(document, "fs_path", "") or ""),
            "file_url": str(getattr(document, "file_url", "") or ""),
            "mime_type": str(getattr(document, "mime_type", "") or ""),
            "name": str(getattr(document, "name", "") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"knowledge:hot:v1:{kind}:{digest}"


async def get_cached_document_text(document: Any) -> str | None:
    ttl = KNOWLEDGE_CONTENT_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    key = document_hot_cache_key(document, "content")
    value = await cache.get(key)
    if not isinstance(value, dict) or value.get("type") != "text":
        return None
    content = value.get("content")
    if not isinstance(content, str):
        return None
    if len(content.encode("utf-8")) > KNOWLEDGE_CACHE_MAX_ENTRY_BYTES:
        return None
    await cache.touch(key, ttl)
    return content


async def cache_document_text(document: Any, content: str) -> bool:
    ttl = KNOWLEDGE_CONTENT_CACHE_TTL_SECONDS
    if ttl <= 0 or not isinstance(content, str):
        return False
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > KNOWLEDGE_CACHE_MAX_ENTRY_BYTES:
        return False
    return await cache.set(
        document_hot_cache_key(document, "content"),
        {"type": "text", "content": content, "size": encoded_size},
        ttl=ttl,
    )


async def get_cached_document_blob(document: Any, kind: str) -> CachedKnowledgeBlob | None:
    ttl = _BLOB_TTLS.get(kind, 0)
    if ttl <= 0:
        return None
    key = document_hot_cache_key(document, kind)
    value = await cache.get(key)
    if not isinstance(value, dict) or value.get("type") != "blob":
        return None
    encoded = value.get("data")
    media_type = value.get("media_type")
    if not isinstance(encoded, str) or not isinstance(media_type, str):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if len(data) > KNOWLEDGE_CACHE_MAX_ENTRY_BYTES:
        return None
    await cache.touch(key, ttl)
    return CachedKnowledgeBlob(data=data, media_type=media_type)


async def cache_document_blob(
    document: Any,
    kind: str,
    data: bytes,
    *,
    media_type: str,
) -> bool:
    ttl = _BLOB_TTLS.get(kind, 0)
    if ttl <= 0 or not data or len(data) > KNOWLEDGE_CACHE_MAX_ENTRY_BYTES:
        return False
    return await cache.set(
        document_hot_cache_key(document, kind),
        {
            "type": "blob",
            "data": base64.b64encode(data).decode("ascii"),
            "media_type": media_type or "application/octet-stream",
            "size": len(data),
        },
        ttl=ttl,
    )


async def cache_document_blob_from_path(
    document: Any,
    kind: str,
    path: str,
    *,
    media_type: str,
) -> bool:
    """Cache a small durable file without blocking the event loop."""
    try:
        size = await asyncio.to_thread(os.path.getsize, path)
    except OSError:
        return False
    if size <= 0 or size > KNOWLEDGE_CACHE_MAX_ENTRY_BYTES:
        return False
    try:
        data = await asyncio.to_thread(_read_bytes, path)
    except OSError:
        return False
    return await cache_document_blob(
        document,
        kind,
        data,
        media_type=media_type,
    )


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
