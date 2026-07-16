"""Artifact store — capability-token-gated download channel.

Browser-runner has no shared mount with the api / JuiceFS knowledge
filesystem (single-shared-container, multi-tenant). When a tool needs
to send a binary back to the api (PDF download, scraped image,
exported CSV), it can't write to /mnt/manor directly. Instead it:

  1. Saves bytes to a per-call tmpdir under ARTIFACT_ROOT
  2. Calls ``store.publish(local_path, mime, suggested_name)`` →
     returns a single-use opaque token
  3. Includes the token in its tool result envelope under ``artifacts``
  4. The api wrapper sees the envelope, calls
     ``GET /artifacts/{token}`` to stream the bytes, writes them under
     the requesting entity's knowledge root, replaces the token in the
     result with the saved knowledge path

Tenant safety
─────────────
- Token is ``secrets.token_urlsafe(32)`` — unguessable.
- Token is **single-use**: GET consumes the registry entry and unlinks
  the on-disk file. Replays return 404.
- The runner stores no entity_id — it doesn't know about tenants. The
  api side decides which entity gets the file (it wraps the call,
  carries the entity_id from its own context).
- Tokens TTL = 5 min by default. A background GC task purges expired
  entries (in-memory + on-disk) every 60 s.
- Filenames provided by the tool are sanitized (basename only, NUL
  stripped, length-clamped). The api side ALSO re-sanitizes before
  writing under /mnt/manor — defense in depth.
- Per-call tmpdir is under ``/tmp/manor-runner-artifacts`` — wiped on
  container restart; mount as tmpfs in compose for hardening if you
  want zero-disk persistence.

Why this and not "stream bytes back in /perform JSON"
─────────────────────────────────────────────────────
JSON+base64 collapses for files >5–10 MB (proxy buffers, FastAPI
default body limits, agent context bloat). The token+fetch pattern
streams the body and keeps the /perform response small and inspectable.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("browser-runner.artifact_store")


_ROOT = Path(os.environ.get("BROWSER_RUNNER_ARTIFACT_ROOT", "/tmp/manor-runner-artifacts"))
_DEFAULT_TTL_SEC = int(os.environ.get("BROWSER_RUNNER_ARTIFACT_TTL_SEC", "300"))
_GC_INTERVAL_SEC = 60
_MAX_FILENAME_LEN = 200


# Restrict filenames to a conservative ASCII set + the most common CJK
# range. The api side re-sanitizes (this layer is just to keep a sane
# on-disk name for debugging).
_FILENAME_OK = re.compile(r"^[A-Za-z0-9._\-一-鿿]+$")


@dataclass
class _Entry:
    token: str
    path: Path
    filename: str
    mime: str
    size: int
    expires_at: float
    consumed: bool = False
    # Caller-side breadcrumb for debugging (not exposed over HTTP).
    note: str = field(default="")


class ArtifactStore:
    """In-memory token registry. Process-local — fine because there's
    only one browser-runner process per replica, and a single tool call
    completes within the same process."""

    def __init__(self) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        _ROOT.mkdir(parents=True, exist_ok=True)
        self._gc_task: Optional[asyncio.Task] = None

    def ensure_gc_running(self) -> None:
        """Lazy GC bootstrap — called on first publish. Avoids needing
        to wire startup hooks into runner.py's ASGI lifecycle."""
        if self._gc_task is None or self._gc_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # not inside an event loop yet — try again next publish
            self._gc_task = loop.create_task(self._gc_loop(), name="artifact-gc")

    async def publish(
        self,
        *,
        src_path: str,
        suggested_name: str,
        mime: str = "",
        ttl_sec: int = _DEFAULT_TTL_SEC,
        note: str = "",
    ) -> Dict[str, object]:
        """Move ``src_path`` into the artifact root under a fresh token
        and register it. Returns the dict that goes in the tool's
        ``artifacts`` envelope."""
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"artifact source not found: {src_path}")
        token = secrets.token_urlsafe(32)
        sanitized_name = _sanitize_filename(suggested_name) or "artifact.bin"
        if not mime:
            mime = mimetypes.guess_type(sanitized_name)[0] or "application/octet-stream"

        dest = _ROOT / token / sanitized_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Move (rename within tmpfs is cheap). If src is on a different
        # device / filesystem, fall back to copy + unlink.
        try:
            os.replace(str(src), str(dest))
        except OSError:
            shutil.copy2(str(src), str(dest))
            try:
                os.unlink(str(src))
            except OSError:
                pass

        size = dest.stat().st_size
        entry = _Entry(
            token=token,
            path=dest,
            filename=sanitized_name,
            mime=mime,
            size=size,
            expires_at=time.time() + max(60, ttl_sec),
            note=note,
        )
        async with self._lock:
            self._entries[token] = entry

        self.ensure_gc_running()
        logger.info(
            "artifact published token=%s name=%s size=%d mime=%s ttl=%ds",
            token[:8] + "...", sanitized_name, size, mime, ttl_sec,
        )
        return {
            "token": token,
            "filename": sanitized_name,
            "size": size,
            "mime": mime,
        }

    async def consume(self, token: str) -> Optional[_Entry]:
        """Atomic check-and-consume. Returns the entry once; subsequent
        calls return None. Caller MUST unlink/serve the file before
        returning to the client (we mark consumed=True so a parallel
        GC pass doesn't double-free)."""
        async with self._lock:
            entry = self._entries.pop(token, None)
            if entry is None:
                return None
            if entry.consumed:
                return None
            if entry.expires_at < time.time():
                # Already expired — delete and signal "not found" rather
                # than serve stale data.
                await asyncio.to_thread(self._cleanup_entry_files, entry)
                return None
            entry.consumed = True
        return entry

    async def _gc_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_GC_INTERVAL_SEC)
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("artifact GC sweep failed")

    async def _sweep(self) -> None:
        now = time.time()
        async with self._lock:
            expired = [t for t, e in self._entries.items() if e.expires_at < now]
            for t in expired:
                entry = self._entries.pop(t, None)
                if entry is not None:
                    await asyncio.to_thread(self._cleanup_entry_files, entry)
        if expired:
            logger.info("artifact GC: purged %d expired entries", len(expired))

    @staticmethod
    def _cleanup_entry_files(entry: _Entry) -> None:
        try:
            entry.path.unlink(missing_ok=True)
        except OSError:
            pass
        # Drop the per-token folder if it's empty.
        try:
            entry.path.parent.rmdir()
        except OSError:
            pass

    # ── Test/debug surface ──────────────────────────────────────────
    def _entry_count(self) -> int:
        return len(self._entries)


def _sanitize_filename(name: str) -> str:
    """basename + strip control chars + length clamp. Producers also
    sanitize, but defense-in-depth keeps the on-disk name predictable."""
    base = os.path.basename((name or "").strip())
    base = base.replace("\x00", "")
    if not base or base in {".", ".."}:
        return ""
    if not _FILENAME_OK.match(base):
        # Fall back to a safe ASCII slug while preserving an extension.
        ext = ""
        if "." in base:
            ext = "." + base.rsplit(".", 1)[-1]
            ext = re.sub(r"[^A-Za-z0-9.]", "", ext)[:10]
        slug = re.sub(r"[^A-Za-z0-9._\-]", "_", base)[:80]
        if not slug or slug in {".", ".."}:
            slug = "artifact"
        base = slug if slug.endswith(ext) else slug + ext
    if len(base) > _MAX_FILENAME_LEN:
        base = base[-_MAX_FILENAME_LEN:]
    return base


# Singleton — providers import this directly.
store = ArtifactStore()
