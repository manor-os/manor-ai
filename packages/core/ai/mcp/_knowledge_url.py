"""Knowledge-path → signed URL conversion for browser-runner inputs.

Browser-runner runs in its own container and has no shared mount with
the api / JuiceFS knowledge filesystem. When an agent passes a
knowledge path (e.g. ``/Photos/foo.jpg``) to a browser tool that needs
the file (e.g. ``browser.publish_media``), the wrapper
on the api side rewrites those paths into short-lived **signed URLs**
that resolve back through the api's existing
``GET /api/v1/fs/public/{token}`` endpoint. Runner then fetches with
its existing ``_fetch_to_tempfile`` helper — no new IO surface needed.

Tenant safety
─────────────
The token (HMAC-SHA256 over ``{entity_id, path, exp}``, see
``packages.core.services.file_access_tokens``) bakes in:

  * the entity that requested the read,
  * the exact relative path (no globbing, no traversal — normalize_rel_path
    rejects anything starting with ``../``),
  * a short expiry (default 5 minutes — enough for a slow upload but not
    long enough to outlive a lease).

A token issued for entity-A cannot read entity-B's file: the verify
endpoint resolves under ``get_entity_root(entity_id)`` which is a
per-tenant directory, and the token's ``entity_id`` claim drives that
lookup directly. Tampering with the claim invalidates the HMAC.

Visibility
──────────
We additionally call ``is_user_visible_path`` *before* signing. The
endpoint trusts the signed claim; the wrapper layer is the gate that
decides whether the user is allowed to ship that file to a browser
tool at all. System paths (``_meta/``, ``.git/``, etc.) are blocked
from being shipped to third-party platforms even when the agent asks.

Usage
─────
    urls = paths_to_signed_urls(
        ["/Photos/cat.jpg", "https://example.com/dog.png"],
        entity_id=entity_id,
    )
    # → ["http://api:8000/api/v1/fs/public/<token>",
    #    "https://example.com/dog.png"]

URLs already starting with ``http://`` / ``https://`` pass through
unchanged. Local /tmp paths from inside the runner container are
rejected — agents shouldn't be passing those, only knowledge-relative
paths.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional
from urllib.parse import unquote

from packages.core.services.file_access_tokens import create_file_access_token
from packages.core.services.file_access_tokens import verify_file_access_token
from packages.core.services.knowledge_visibility import (
    is_user_visible_path,
    normalize_rel_path,
)

logger = logging.getLogger(__name__)


# URL the browser-runner uses to reach back to the api for signed-file
# fetches. Defaults to the docker-compose service hostname; override for
# multi-host deployments. The PUBLIC api hostname (e.g. manor.ai) would
# also work but is slower (extra TLS hop) and exposes intermediate
# routers to the file traffic.
_INTERNAL_API_URL = os.environ.get(
    "MANOR_INTERNAL_API_URL", "http://api:8000",
).rstrip("/")


# Default 5-minute TTL — long enough for the runner to fetch a multi-MB
# image over a slow connection, short enough that a leaked token expires
# before the next agent turn.
_DEFAULT_TTL_SECONDS = 300


class KnowledgePathError(ValueError):
    """Raised when a path can't be turned into a fetchable URL."""


def paths_to_signed_urls(
    paths: List[str],
    *,
    entity_id: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    base_url: str | None = None,
) -> List[str]:
    """Convert a mixed list of paths/URLs into URLs the runner can GET.

    - ``http://`` / ``https://`` URLs pass through.
    - Knowledge-relative paths (``/Photos/foo.jpg`` or ``Photos/foo.jpg``)
      are signed for ``entity_id`` and rewritten to the internal api URL.
    - Anything else (absolute /tmp, relative `../`, empty) raises
      KnowledgePathError so the agent gets a structured error rather
      than a silent "did not render".
    """
    if not entity_id:
        raise KnowledgePathError("entity_id is required to sign knowledge URLs")
    out: List[str] = []
    for raw in paths or []:
        url = _one(raw, entity_id=entity_id, ttl_seconds=ttl_seconds, base_url=base_url)
        out.append(url)
    return out


def _one(raw: str, *, entity_id: str, ttl_seconds: int, base_url: str | None = None) -> str:
    s = (raw or "").strip()
    if not s:
        raise KnowledgePathError("empty path in image_paths")
    local_fs_rel = _rel_path_from_local_fs_url(s, entity_id=entity_id)
    if local_fs_rel:
        s = local_fs_rel
    if s.startswith(("http://", "https://")):
        return s

    # Treat as a knowledge-relative path. normalize_rel_path also
    # rejects parent-traversal, NUL bytes, and other path-injection
    # tricks — the same gate the in-app /fs/read endpoint uses.
    rel = normalize_rel_path(s.lstrip("/"))
    if not rel or rel.startswith("../") or rel == "..":
        raise KnowledgePathError(f"path traversal rejected: {raw!r}")
    if not is_user_visible_path(rel):
        raise KnowledgePathError(
            f"path is hidden / system-only and cannot be sent to a browser tool: {raw!r}"
        )

    token = create_file_access_token(
        entity_id=entity_id,
        rel_path=rel,
        expires_in_seconds=ttl_seconds,
    )
    return f"{_api_origin(base_url)}/api/v1/fs/public/{token}"


def _api_origin(base_url: str | None = None) -> str:
    base = (base_url or _INTERNAL_API_URL).strip().rstrip("/")
    if base.endswith("/api/v1"):
        base = base[:-7]
    elif base.endswith("/api"):
        base = base[:-4]
    return base.rstrip("/")


def _rel_path_from_local_fs_url(value: str, *, entity_id: str) -> str | None:
    """Normalize Manor-local /api/v1/fs references back to entity paths.

    Generated media tools return relative app URLs such as
    ``/api/v1/fs/{entity_id}/Knowledge/Campaigns/launch/cover.png``. Browser
    runner cannot fetch those directly, so convert them to canonical
    Knowledge-relative paths before signing.
    """
    path = str(value or "").strip()
    if not path.startswith("/api/v1/fs/"):
        return None
    if path.startswith("/api/v1/fs/public/"):
        token_part = path.split("/", 5)[5] if len(path.split("/", 5)) >= 6 else ""
        token = token_part.split("/", 1)[0]
        payload = verify_file_access_token(token) if token else None
        if payload and payload.get("entity_id") == entity_id:
            return normalize_rel_path(str(payload.get("path") or ""))
        raise KnowledgePathError("local fs public URL token is expired or belongs to another entity")

    parts = path.split("/", 5)
    if len(parts) < 6:
        raise KnowledgePathError(f"invalid local fs URL: {value!r}")
    if parts[4] != entity_id:
        raise KnowledgePathError("local fs URL belongs to another entity")
    return normalize_rel_path(unquote(parts[5]))


def safe_paths_to_signed_urls(
    paths: List[str],
    *,
    entity_id: Optional[str],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    base_url: str | None = None,
) -> tuple[Optional[List[str]], Optional[str]]:
    """Same as ``paths_to_signed_urls`` but returns ``(urls, error_msg)``
    for use in MCP wrappers where we want to surface a structured
    isError result instead of an exception."""
    if not entity_id:
        return None, (
            "Internal: entity_id is missing in the call context; "
            "knowledge files cannot be signed for browser-runner."
        )
    try:
        return paths_to_signed_urls(
            paths,
            entity_id=entity_id,
            ttl_seconds=ttl_seconds,
            base_url=base_url,
        ), None
    except KnowledgePathError as exc:
        return None, str(exc)


def safe_paths_to_signed_url_candidates(
    paths: List[str],
    *,
    entity_id: Optional[str],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    base_urls: List[str | None] | None = None,
) -> tuple[Optional[List[List[str]]], Optional[str]]:
    """Return ordered, de-duplicated signed URL candidates per input path.

    Local CLI workers ultimately upload media from disk. They first need to
    download each Knowledge file from the paired Manor API, and a single public
    base URL can be stale in mixed staging/local deployments. Supplying multiple
    base URLs lets the local worker try the API origin it is actually paired to
    before declaring the asset missing.
    """
    if not entity_id:
        return None, (
            "Internal: entity_id is missing in the call context; "
            "knowledge files cannot be signed for browser-runner."
        )
    normalized_paths = list(paths or [])
    candidates: List[List[str]] = [[] for _ in normalized_paths]
    errors: List[str] = []
    for base_url in _dedupe_base_urls(base_urls):
        urls, err = safe_paths_to_signed_urls(
            normalized_paths,
            entity_id=entity_id,
            ttl_seconds=ttl_seconds,
            base_url=base_url,
        )
        if err:
            errors.append(err)
            continue
        for idx, url in enumerate(urls or []):
            if url and url not in candidates[idx]:
                candidates[idx].append(url)
    if any(not item for item in candidates):
        detail = f": {errors[0]}" if errors else ""
        return None, f"Could not create downloadable media URLs for all draft assets{detail}"
    return candidates, None


def _dedupe_base_urls(base_urls: List[str | None] | None) -> List[str | None]:
    raw_values = base_urls if base_urls is not None else [None]
    out: List[str | None] = []
    seen: set[str] = set()
    for raw in raw_values:
        base = raw.strip() if isinstance(raw, str) else raw
        key = base or "<internal>"
        if key in seen:
            continue
        seen.add(key)
        out.append(base)
    return out
