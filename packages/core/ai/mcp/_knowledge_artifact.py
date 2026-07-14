"""Browser → knowledge: fetch runner artifacts and write into /mnt/manor.

Inverse of ``_knowledge_url`` (which signs knowledge files for the
runner to read). When a browser tool produces a binary (PDF download,
scraped image, exported CSV), the runner publishes it to its in-memory
artifact store and includes a one-time token in the tool result:

    {
      ...other fields...,
      "artifacts": [
        {"token": "abc...", "filename": "post.pdf", "size": 12345, "mime": "application/pdf"}
      ]
    }

The wrapper here detects this envelope, fetches each token over HTTP
to ``BROWSER_RUNNER_URL/artifacts/{token}``, and writes the bytes to
``/mnt/manor/{entity_id}/{folder}/{filename}``. The result envelope is
mutated in place: each artifact dict gains a ``saved_to`` field with
the knowledge-relative path. The token is dropped (one-time, already
consumed by the fetch).

Tenant safety
─────────────
- The token is the runner's capability — anyone with the token gets the
  bytes once. The wrapper is the ONLY place that reads tokens, and it
  always writes under ``get_entity_root(entity_id)``. Cross-tenant
  exposure requires that the wrapper be called with someone else's
  entity_id, which is set by the MCP dispatcher before the call (the
  same context that signs URLs in PR-A).
- Filenames are sanitized at TWO layers (runner + here). Path traversal
  via filename is caught by ``_safe_dest_under_root``: the resolved
  path must stay under entity_root.
- Folder is sanitized: only ASCII / digits / dash / underscore / slash;
  no leading slash; no ``..``. Defaults to ``Browser Downloads/{provider}``
  when the tool didn't specify.
- Collisions get a numeric suffix (``foo (2).pdf``) — never overwrite
  pre-existing knowledge files.

Tools opt in by including the ``artifacts`` envelope in their result
(no other change). Tools that don't produce binaries see no behavior
change.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from packages.core.services.entity_fs import (
    EntityFilesystemError,
    assert_entity_filesystem_ready,
    copy_entity_file_atomic,
    get_entity_root,
)

logger = logging.getLogger(__name__)


_RUNNER_URL = os.environ.get("BROWSER_RUNNER_URL", "http://browser-runner:5200").rstrip("/")
_RUNNER_TOKEN = os.environ.get("BROWSER_RUNNER_TOKEN", "").strip()

# 100 MB per artifact — generous; tighter than the runner's
# /perform 16 MB inline limit because we stream this directly to disk.
# Bump for video pipelines if needed.
_MAX_ARTIFACT_BYTES = int(os.environ.get("MANOR_RUNNER_ARTIFACT_MAX_BYTES", str(100 * 1024 * 1024)))

_FOLDER_SAFE = re.compile(r"^[A-Za-z0-9 _\-./一-鿿]+$")


class ArtifactDownloadError(RuntimeError):
    pass


async def process_result_artifacts(
    result: Dict[str, Any],
    *,
    entity_id: str,
    provider: str,
    target_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """If ``result`` carries an ``artifacts`` list, fetch each one from
    the runner and write to the entity's knowledge filesystem. Mutates
    the artifact entries in place: drops ``token`` and adds ``saved_to``.

    Returns the mutated result. If there are no artifacts, returns it
    unchanged.
    """
    if not isinstance(result, dict):
        return result
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return result
    if not entity_id:
        # Don't silently drop the bytes — surface a structured error
        # the agent can see. The runner has already consumed the
        # tokens by the time we get here, so retry won't help; this is
        # a wiring bug in the dispatcher.
        for art in artifacts:
            art["error"] = "missing entity_id in call context — could not save to knowledge"
            art.pop("token", None)
        return result

    try:
        assert_entity_filesystem_ready()
    except EntityFilesystemError as exc:
        for art in artifacts:
            art["error"] = f"entity filesystem unavailable: {exc}"
            art.pop("token", None)
        return result

    folder = _normalize_folder(target_folder or f"Browser Downloads/{provider}")
    entity_root = Path(get_entity_root(entity_id)).resolve()
    dest_dir = entity_root / folder
    dest_dir.mkdir(parents=True, exist_ok=True)

    headers: Dict[str, str] = {}
    if _RUNNER_TOKEN:
        headers["Authorization"] = f"Bearer {_RUNNER_TOKEN}"

    async with httpx.AsyncClient(timeout=120.0, headers=headers) as cx:
        for art in artifacts:
            token = (art.get("token") or "").strip()
            filename = (art.get("filename") or "artifact.bin").strip()
            mime = (art.get("mime") or "").strip()
            if not token:
                art["error"] = "artifact entry missing token"
                continue
            try:
                saved_rel = await _fetch_one(
                    cx, token, entity_id=entity_id, dest_dir=dest_dir,
                    entity_root=entity_root,
                    suggested_filename=filename,
                )
                art["saved_to"] = saved_rel
                art.pop("token", None)
                logger.info(
                    "artifact saved provider=%s entity=%s saved_to=%s mime=%s",
                    provider, entity_id, saved_rel, mime,
                )
            except ArtifactDownloadError as exc:
                art["error"] = str(exc)
                art.pop("token", None)
            except Exception as exc:  # noqa: BLE001
                logger.exception("artifact processing failed token=%s", token[:8] + "...")
                art["error"] = f"artifact write failed: {exc}"
                art.pop("token", None)
    return result


async def _fetch_one(
    cx: httpx.AsyncClient,
    token: str,
    *,
    entity_id: str,
    dest_dir: Path,
    entity_root: Path,
    suggested_filename: str,
) -> str:
    """Stream one artifact to disk under ``dest_dir``. Returns the
    knowledge-relative path that was written (string)."""
    url = f"{_RUNNER_URL}/artifacts/{token}"
    sanitized = _sanitize_filename(suggested_filename)
    final_path = _resolve_unique(dest_dir, sanitized)

    # Defense-in-depth: even though dest_dir is built from entity_root +
    # validated folder, double-check the resolved final_path stays
    # under entity_root.
    try:
        final_path.resolve().relative_to(entity_root)
    except ValueError:
        raise ArtifactDownloadError(f"refusing to write outside entity root: {final_path}")

    rel_path = str(final_path.relative_to(entity_root)).replace(os.sep, "/")
    written = 0
    tmp_path: str | None = None
    async with cx.stream("GET", url) as resp:
        if resp.status_code == 404:
            raise ArtifactDownloadError("artifact expired or already consumed")
        if resp.status_code != 200:
            body = await resp.aread()
            raise ArtifactDownloadError(
                f"runner returned HTTP {resp.status_code}: {body[:200]!r}"
            )
        fd, tmp_path = tempfile.mkstemp(prefix="manor-artifact-", suffix=".tmp")
        os.close(fd)
        try:
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    written += len(chunk)
                    if written > _MAX_ARTIFACT_BYTES:
                        raise ArtifactDownloadError(
                            f"artifact exceeded {_MAX_ARTIFACT_BYTES} byte cap"
                        )
                    f.write(chunk)
            copy_entity_file_atomic(
                entity_id,
                rel_path,
                tmp_path,
                expected_size=written,
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    return rel_path


def _normalize_folder(folder: str) -> str:
    """Collapse to a safe relative folder under entity_root.

    Rejects parent traversal, NUL bytes, and characters outside an
    ASCII + digits + dash/underscore/slash + CJK whitelist."""
    f = (folder or "").strip().strip("/")
    if not f:
        f = "Browser Downloads"
    if ".." in f.split("/"):
        f = "Browser Downloads"
    if "\x00" in f:
        f = "Browser Downloads"
    if not _FOLDER_SAFE.match(f):
        # Slug each segment as a fallback so user-typed Chinese
        # folders (e.g. "下载") still go through.
        clean = []
        for part in f.split("/"):
            slug = re.sub(r"[^A-Za-z0-9._\-一-鿿]", "_", part).strip("._")
            if slug and slug not in {".", ".."}:
                clean.append(slug)
        f = "/".join(clean) or "Browser Downloads"
    return f


def _sanitize_filename(name: str) -> str:
    base = os.path.basename((name or "").strip())
    base = base.replace("\x00", "")
    if not base or base in {".", ".."}:
        return "artifact.bin"
    if len(base) > 200:
        base = base[-200:]
    return base


def _resolve_unique(dest_dir: Path, filename: str) -> Path:
    """Pick a non-colliding filename. ``foo.pdf`` → ``foo (2).pdf`` →
    ``foo (3).pdf`` etc. Caps at 99 to avoid runaway loops on shared
    folders."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for i in range(2, 100):
        candidate = dest_dir / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise ArtifactDownloadError(
        f"too many naming collisions in {dest_dir} for {filename!r}"
    )


def parse_target_folder_from_args(args: Dict[str, Any]) -> Optional[str]:
    """Convenience: tools can accept a ``save_to`` arg to override
    where their artifacts land. Returns the normalized folder or None."""
    raw = args.get("save_to") if isinstance(args, dict) else None
    if not raw:
        return None
    return _normalize_folder(str(raw))
