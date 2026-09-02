"""
Entity Filesystem API — REST endpoints for the JuiceFS-backed knowledge filesystem.

Provides file browser operations for the frontend:
  GET  /api/v1/fs/list          — list directory contents
  GET  /api/v1/fs/tree          — full directory tree (for sidebar)
  GET  /api/v1/fs/read          — read file content
  GET  /api/v1/fs/info          — file metadata
  POST /api/v1/fs/write         — create/update file
  POST /api/v1/fs/mkdir         — create directory
  POST /api/v1/fs/move          — move/rename file or directory
  POST /api/v1/fs/delete        — delete file or directory
  POST /api/v1/fs/upload        — upload binary file
  GET  /api/v1/fs/search        — search file contents (ripgrep)
  GET  /api/v1/fs/wiki-links    — resolve [[wiki links]] in a markdown file
  GET  /api/v1/fs/lint          — knowledge base health check

All endpoints require JWT auth. Entity is resolved from the current user's token.
Paths are relative to /mnt/manor/{entity_id}/.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json as json_mod
import logging
import mimetypes
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path as _Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response as RawResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import (
    authenticated_user_credential_from_claims,
    get_current_user,
    require_workspace_readable,
)
from apps.api.file_responses import (
    EntitySnapshotFileResponse as _ReadLockedFileResponse,
    entity_filesystem_read_boundary as _entity_filesystem_read_boundary,
)
from packages.core.database import get_db
from packages.core.models.document import Document
from packages.core.models.staff import Staff
from packages.core.models.user import User
from packages.core.services.actor_authorization import (
    AuthenticatedUserCredential,
    resolve_current_user_actor,
)
from packages.core.services.permission_gate import ResourcePermissionGate
from packages.core.services.filesystem_access import (
    FilesystemAccessDenied,
    require_directory_write_access as require_fs_directory_write_access,
    require_path_mutation_access as require_fs_path_mutation_access,
    require_path_write_access as require_fs_path_write_access,
)
from packages.core.services.entity_fs import (
    EntityFilesystemError,
    EntityFilesystemBusyError,
    EntityFileWriteLockMode,
    EditorWriteIntentStatus,
    SYSTEM_DIRS,
    SYSTEM_FILES,
    assert_entity_filesystem_ready,
    copy_entity_file_atomic,
    claim_editor_write_intent,
    entity_filesystem_mutation_lock,
    finish_entity_filesystem_mutation,
    get_entity_root,
    is_fs_enabled,
    is_system_path,
    mark_editor_write_intent_committed,
    resolve_path,
    append_log,
    write_entity_file_atomic,
)
from packages.core.services.knowledge_visibility import (
    is_user_visible_folder_path,
    is_user_visible_path,
    normalize_rel_path,
)
from packages.core.services.file_access_tokens import verify_file_access_token
from packages.core.permissions import user_is_effective_entity_admin

logger = logging.getLogger(__name__)

_finish_filesystem_mutation = finish_entity_filesystem_mutation
_PATH_WRITE_LOCK_RETRY_SECONDS = 0.01
_PATH_WRITE_LOCK_TIMEOUT_SECONDS = 5.0

router = APIRouter(prefix="/api/v1/fs", tags=["filesystem"])


_ACTIVE_PUBLIC_EXTENSIONS = {
    ".html", ".htm", ".svg", ".xml", ".xhtml", ".js", ".mjs", ".css",
}
_ACTIVE_PUBLIC_MEDIA_TYPES = {
    "text/html", "image/svg+xml", "application/xml", "text/xml",
    "application/javascript", "text/javascript", "text/css",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_fs():
    """Raise 503 if filesystem is not enabled."""
    if not is_fs_enabled():
        raise HTTPException(503, "Entity filesystem not enabled (MANOR_FS_ENABLED=false)")


def _require_fs_ready_for_mutation() -> None:
    _require_fs()
    try:
        assert_entity_filesystem_ready()
    except EntityFilesystemError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Entity filesystem is temporarily unavailable: {exc}",
        ) from exc


@asynccontextmanager
async def _entity_filesystem_mutation_boundary(root: str):
    """Wait cancellably for the entity mutation lock, mapping contention to 423."""
    try:
        async with entity_filesystem_mutation_lock(root):
            yield
    except EntityFilesystemBusyError as exc:
        raise HTTPException(
            status_code=423,
            detail="Entity filesystem is busy with another mutation; retry shortly",
        ) from exc


def _try_acquire_path_write_lock(root: str, rel_path: str):
    """Try once to acquire a cross-process lock without blocking a worker."""
    lock_dir = os.path.join(root, ".ai", "write-locks")
    os.makedirs(lock_dir, exist_ok=True)
    path_hash = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()
    handle = open(os.path.join(lock_dir, f"{path_hash}.lock"), "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except Exception:
        handle.close()
        raise
    return handle


async def _acquire_path_write_lock(
    root: str,
    rel_path: str,
    *,
    timeout_seconds: float = _PATH_WRITE_LOCK_TIMEOUT_SECONDS,
):
    """Acquire one cancellable, bounded path lock without blocking a worker."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0)
    while True:
        handle = await _try_acquire_path_write_lock_cancellably(root, rel_path)
        if handle is not None:
            return handle
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise HTTPException(
                status_code=423,
                detail="File is busy with another mutation; retry shortly",
            )
        await asyncio.sleep(min(_PATH_WRITE_LOCK_RETRY_SECONDS, remaining))


def _release_path_write_lock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _release_abandoned_path_write_lock(attempt: asyncio.Task) -> None:
    """Release a lock acquired after its awaiting request was cancelled."""
    try:
        handle = attempt.result()
    except BaseException:
        return
    if handle is not None:
        _release_path_write_lock(handle)


async def _try_acquire_path_write_lock_cancellably(root: str, rel_path: str):
    attempt = asyncio.create_task(asyncio.to_thread(
        _try_acquire_path_write_lock,
        root,
        rel_path,
    ))
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        # asyncio cannot stop a worker thread already performing the nonblocking
        # filesystem call. Clean up if that thread wins the lock after cancellation.
        attempt.add_done_callback(_release_abandoned_path_write_lock)
        raise


async def _acquire_path_mutation_locks(
    root: str,
    *rel_paths: str,
    timeout_seconds: float = _PATH_WRITE_LOCK_TIMEOUT_SECONDS,
):
    """Acquire path locks in stable order without leaking partial acquisitions."""
    handles = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_seconds, 0)
    try:
        for rel_path in sorted(set(rel_paths)):
            handles.append(await _acquire_path_write_lock(
                root,
                rel_path,
                timeout_seconds=max(deadline - loop.time(), 0),
            ))
    except BaseException:
        for handle in reversed(handles):
            _release_path_write_lock(handle)
        raise
    return handles


def _release_path_mutation_locks(handles) -> None:
    for handle in reversed(handles):
        _release_path_write_lock(handle)


def _file_not_found() -> HTTPException:
    """Return an uncacheable 404 for file-serving misses."""
    return HTTPException(404, "File not found", headers={"Cache-Control": "no-store"})


async def _require_path_write_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
    exists: bool,
) -> None:
    try:
        await require_fs_path_write_access(
            db,
            user=user,
            rel_path=rel_path,
            exists=exists,
        )
    except FilesystemAccessDenied as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


async def _require_directory_write_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
) -> None:
    try:
        await require_fs_directory_write_access(
            db,
            user=user,
            rel_path=rel_path,
        )
    except FilesystemAccessDenied as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


async def _require_path_mutation_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
    full_path: str,
    action: str,
    destination_rel_path: str | None = None,
) -> list[Document]:
    try:
        return await require_fs_path_mutation_access(
            db,
            user=user,
            rel_path=rel_path,
            full_path=full_path,
            entity_root=_entity_root(user.entity_id),
            action=action,
            destination_rel_path=destination_rel_path,
        )
    except FilesystemAccessDenied as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _signed_file_response_metadata(
    file_path: _Path,
    content_type: str,
) -> tuple[str, dict[str, str]]:
    headers = {
        "Cache-Control": "private, max-age=900",
        "X-Content-Type-Options": "nosniff",
    }
    if (
        file_path.suffix.lower() in _ACTIVE_PUBLIC_EXTENSIONS
        or content_type.split(";", 1)[0].lower() in _ACTIVE_PUBLIC_MEDIA_TYPES
    ):
        safe_name = file_path.name.replace('"', "").replace("\r", "").replace("\n", "")
        headers["Content-Disposition"] = (
            f"attachment; filename=\"{safe_name}\"; "
            f"filename*=UTF-8''{quote(file_path.name)}"
        )
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return "application/octet-stream", headers
    return content_type, headers


# Strong refs to the in-flight avatar cleanup tasks so they don't get
# garbage-collected before the event loop runs them. asyncio.create_task
# only holds a weak reference; without this set, scheduled-but-not-yet-
# started tasks can be GC'd if there are no other references.
_PENDING_AVATAR_CLEANUP_TASKS: set[asyncio.Task[None]] = set()


async def _clear_stale_avatar_url(avatar_url: str) -> None:
    """Background task — null out User / Staff rows pointing at a now-missing
    avatar file.

    Called when GET /api/v1/fs/{entity}/avatars/<file> returns 404 because
    the file no longer exists on disk (common in dev after FS resets, or
    after avatar files were manually deleted). Frontend #121 already shows
    initials fallback on the broken <img>, but the DB still hands out the
    stale URL on every subsequent staff/user fetch — every fresh page load
    fires another 404. Clearing the column here means after the first 404,
    subsequent fetches return avatar_url=null and the frontend short-
    circuits to initials directly, no /fs request at all.

    Uses its own AsyncSession (not the request's `db`) because we run
    *after* the response has been sent — the request's session is already
    closed by then. Imports ``async_session`` lazily so test fixtures can
    monkey-patch ``packages.core.database.async_session`` without our
    module having already captured the production reference at import.
    """
    from packages.core.database import async_session  # lazy — see docstring

    async with async_session() as cleanup_db:
        try:
            await cleanup_db.execute(
                update(User)
                .where(User.avatar_url == avatar_url)
                .values(avatar_url=None)
            )
            await cleanup_db.execute(
                update(Staff)
                .where(Staff.avatar_url == avatar_url)
                .values(avatar_url=None)
            )
            await cleanup_db.commit()
        except Exception:
            # Best-effort cleanup: don't crash the background task if the
            # DB is briefly unavailable; the next stale-URL request will
            # try again.
            await cleanup_db.rollback()
            logger.exception("stale avatar_url cleanup failed for %s", avatar_url)



def _entity_root(entity_id: str) -> str:
    """Resolve and validate entity root path."""
    root = get_entity_root(entity_id)
    if not os.path.isdir(root):
        raise HTTPException(404, "Entity filesystem not provisioned")
    return root


def _resolve(entity_id: str, rel_path: str) -> str:
    """Resolve relative path within entity root. Prevents traversal."""
    result = resolve_path(entity_id, rel_path.lstrip("/"))
    if result is None:
        raise HTTPException(403, "Path traversal not allowed")
    return result


def _is_hidden(name: str, show_system: bool = False) -> bool:
    """Check if a file/directory should be hidden from user view."""
    if name.startswith("."):
        return True
    if show_system:
        return False
    return name in SYSTEM_FILES or name in SYSTEM_DIRS


def _is_visible_browser_item(full_path: str, root: str, *, show_system: bool = False) -> bool:
    if show_system:
        return True
    rel = normalize_rel_path(os.path.relpath(full_path, root))
    if os.path.isdir(full_path):
        return is_user_visible_folder_path(rel)
    return is_user_visible_path(rel)


def _is_visible_directory_rel(rel_path: str) -> bool:
    rel = normalize_rel_path(rel_path)
    return rel == "" or is_user_visible_folder_path(rel)


def _assert_user_visible_rel(rel_path: str, *, is_dir: bool, action: str) -> None:
    visible = _is_visible_directory_rel(rel_path) if is_dir else is_user_visible_path(rel_path)
    if not visible:
        raise HTTPException(403, f"Cannot {action} hidden/system path")


async def _unreadable_doc_paths(
    db: AsyncSession,
    entity_id: str,
    rel_paths: list[str],
    user: User,
    *,
    directory_paths: list[str] | None = None,
    credential: AuthenticatedUserCredential | None = None,
) -> set[str]:
    """Apply the canonical Knowledge/Workspace ACL to raw filesystem paths."""
    credential = credential or AuthenticatedUserCredential.from_user(user)
    if credential.entity_id != str(entity_id):
        raise _file_not_found()
    blocked: set[str] = set()
    directories = directory_paths or []
    if not rel_paths and not directories:
        authorized = await ResourcePermissionGate.authorize_filesystem_path_batch(
            db,
            credential=credential,
            rel_paths=[],
        )
        if authorized is None:
            raise _file_not_found()
        return blocked
    for start in range(0, len(rel_paths), 400):
        batch = rel_paths[start:start + 400]
        authorized = await ResourcePermissionGate.authorize_filesystem_path_batch(
            db,
            credential=credential,
            rel_paths=batch,
        )
        if authorized is None:
            raise _file_not_found()
        blocked.update(authorized.unreadable_paths)
    for start in range(0, len(directories), 400):
        batch = directories[start:start + 400]
        authorized = await ResourcePermissionGate.authorize_filesystem_path_batch(
            db,
            credential=credential,
            rel_paths=[],
            directory_paths=batch,
        )
        if authorized is None:
            raise _file_not_found()
        blocked.update(authorized.unreadable_paths)
    return blocked


async def _assert_path_readable(
    db: AsyncSession,
    entity_id: str,
    rel_path: str,
    user: User,
    *,
    is_dir: bool = False,
    credential: AuthenticatedUserCredential | None = None,
) -> None:
    """404 if ``rel_path`` is a Knowledge document the caller cannot read.

    Raises the same not-found error as a missing file so the endpoint never
    confirms the existence of a private document to an unauthorized member.
    """
    if await _unreadable_doc_paths(
        db,
        entity_id,
        [] if is_dir else [rel_path],
        user,
        directory_paths=[rel_path] if is_dir else None,
        credential=credential,
    ):
        raise _file_not_found()


_PUBLIC_RAW_PREFIXES: tuple[str, ...] = (
    "avatars/",
    # Files under this prefix are explicit Marketplace publication copies,
    # never the original private Workspace/Knowledge artifacts.
    "marketplace/blueprints/",
)


def _is_public_raw_file_path(rel_path: str) -> bool:
    rel = normalize_rel_path(rel_path)
    return any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in _PUBLIC_RAW_PREFIXES)


async def _optional_user_from_bearer(
    request: Request,
    db: AsyncSession,
) -> tuple[Any, AuthenticatedUserCredential] | None:
    """Best-effort auth for raw file URLs.

    Raw file serving needs a public exception for avatars, but every
    user/Knowledge artifact should require the same bearer token as the REST
    API. This helper avoids making avatar URLs require auth.
    """
    auth = request.headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    from packages.core.services.auth_service import decode_token, get_user_by_id

    claims = decode_token(token.strip())
    user_id = claims.get("sub") if claims else None
    if not user_id:
        return None
    user = await get_user_by_id(db, user_id)
    token_entity_id = str(claims.get("entity_id") or getattr(user, "entity_id", "") or "")
    credential = authenticated_user_credential_from_claims(
        user_id=str(user_id),
        entity_id=token_entity_id,
        claims=claims,
    )
    actor = await resolve_current_user_actor(db, credential)
    if not user or actor is None:
        return None
    return (
        SimpleNamespace(
            id=actor.user_id,
            entity_id=actor.entity_id,
            role=actor.role,
            token_version=credential.token_version,
        ),
        credential,
    )


def _file_info(full_path: str, root: str) -> dict[str, Any]:
    """Build file metadata dict."""
    rel = os.path.relpath(full_path, root)
    name = os.path.basename(full_path)
    is_dir = os.path.isdir(full_path)
    try:
        stat = os.stat(full_path)
        return {
            "name": name,
            "path": rel,
            "type": "directory" if is_dir else "file",
            "size": stat.st_size if not is_dir else None,
            # A raw child count leaks the existence of entries removed by the
            # Document/folder ACL pass. Callers can derive authorized children
            # by listing the directory instead.
            "item_count": None,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
            "mime_type": mimetypes.guess_type(name)[0] if not is_dir else None,
            "extension": os.path.splitext(name)[1] if not is_dir else None,
            "is_system": is_system_path(name),
        }
    except OSError:
        return {"name": name, "path": rel, "type": "unknown", "is_system": is_system_path(name)}


# ── Models ───────────────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    path: str
    content: str
    save_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    save_sequence: int | None = Field(default=None, ge=1, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def validate_save_intent(self):
        if (self.save_session_id is None) != (self.save_sequence is None):
            raise ValueError("save_session_id and save_sequence must be provided together")
        return self


class MkdirRequest(BaseModel):
    path: str


class MoveRequest(BaseModel):
    src: str
    dest: str


class DeleteRequest(BaseModel):
    path: str


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/list")
async def list_directory(
    user: User = Depends(get_current_user),
    path: str = Query(".", description="Relative path from entity root"),
    show_system: bool = Query(False, description="Show system files (MANOR.md, index.md, log.md)"),
    db: AsyncSession = Depends(get_db),
):
    """List contents of a directory."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    root = _entity_root(user.entity_id)
    full = _resolve(user.entity_id, path)
    rel = normalize_rel_path(os.path.relpath(full, root))
    if not show_system:
        _assert_user_visible_rel(rel, is_dir=True, action="list")

    def _list():
        if not os.path.isdir(full):
            return None
        items = []
        for name in sorted(os.listdir(full)):
            item_path = os.path.join(full, name)
            if _is_hidden(name, show_system) or not _is_visible_browser_item(item_path, root, show_system=show_system):
                continue
            items.append(_file_info(item_path, root))
        items.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))
        return items

    async with _entity_filesystem_read_boundary(root):
        if rel:
            await _assert_path_readable(
                db, user.entity_id, rel, user, is_dir=True
                , credential=credential
            )
        items = await asyncio.to_thread(_list)
        if items is None:
            raise HTTPException(404, f"Directory not found: {path}")
        # Drop files that map to a Knowledge document the caller cannot read.
        blocked = await _unreadable_doc_paths(
            db,
            user.entity_id,
            [i["path"] for i in items if i["type"] != "directory"],
            user,
            directory_paths=[i["path"] for i in items if i["type"] == "directory"],
            credential=credential,
        )
        if blocked:
            items = [i for i in items if normalize_rel_path(i["path"]) not in blocked]
        return {"items": items, "path": os.path.relpath(full, root), "count": len(items)}


@router.get("/tree")
async def directory_tree(
    user: User = Depends(get_current_user),
    max_depth: int = Query(3, ge=1, le=10),
    show_system: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Full directory tree for sidebar file browser."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    root = _entity_root(user.entity_id)

    def scan_level(
        parents: list[tuple[str, list[dict[str, Any]]]],
    ) -> list[tuple[list[dict[str, Any]], dict[str, Any], str]]:
        candidates = []
        for dir_path, target in parents:
            try:
                with os.scandir(dir_path) as entries:
                    level_entries = sorted(entries, key=lambda entry: entry.name.lower())
            except OSError:
                continue
            nodes = []
            for entry in level_entries:
                name = entry.name
                if _is_hidden(name, show_system):
                    continue
                full = entry.path
                if not _is_visible_browser_item(
                    full,
                    root,
                    show_system=show_system,
                ):
                    continue
                rel = os.path.relpath(full, root)
                is_dir = entry.is_dir(follow_symlinks=False)
                node: dict[str, Any] = {
                    "name": name,
                    "path": rel,
                    "type": "directory" if is_dir else "file",
                }
                if is_dir:
                    node["children"] = []
                else:
                    node["extension"] = os.path.splitext(name)[1]
                nodes.append((target, node, full))
            nodes.sort(key=lambda item: (
                0 if item[1]["type"] == "directory" else 1,
                item[1]["name"].lower(),
            ))
            candidates.extend(nodes)
        return candidates

    async with _entity_filesystem_read_boundary(root):
        tree: list[dict[str, Any]] = []
        parents = [(root, tree)]
        for _depth in range(1, max_depth + 1):
            candidates = await asyncio.to_thread(scan_level, parents)
            blocked = await _unreadable_doc_paths(
                db,
                user.entity_id,
                [
                    node["path"]
                    for _target, node, _full in candidates
                    if node["type"] == "file"
                ],
                user,
                directory_paths=[
                    node["path"]
                    for _target, node, _full in candidates
                    if node["type"] == "directory"
                ],
                credential=credential,
            )
            next_parents = []
            for target, node, full in candidates:
                if normalize_rel_path(node["path"]) in blocked:
                    continue
                target.append(node)
                if node["type"] == "directory":
                    next_parents.append((full, node["children"]))
            parents = next_parents
            if not parents:
                break
        return {"tree": tree, "entity_id": user.entity_id}


@router.get("/read")
async def read_file(
    user: User = Depends(get_current_user),
    path: str = Query(..., description="Relative path to file"),
    db: AsyncSession = Depends(get_db),
):
    """Read file content. Returns text for text files, base64 for binary."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    full = _resolve(user.entity_id, path)
    root = _entity_root(user.entity_id)
    rel = normalize_rel_path(os.path.relpath(full, root))
    _assert_user_visible_rel(rel, is_dir=False, action="read")
    def _read():
        if not os.path.isfile(full):
            return None
        stat = os.stat(full)
        mime = mimetypes.guess_type(os.path.basename(full))[0] or ""
        meta = {
            "path": path,
            "size": stat.st_size,
            "mime_type": mime,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }
        if mime.startswith("text/") or full.endswith((".md", ".json", ".csv", ".txt", ".html", ".xml", ".yaml", ".yml")):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
                return {**meta, "content": content, "encoding": "utf-8"}
            except UnicodeDecodeError:
                pass
        with open(full, "rb") as f:
            data = f.read()
        return {**meta, "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}

    async with _entity_filesystem_read_boundary(root):
        await _assert_path_readable(
            db, user.entity_id, rel, user, credential=credential,
        )
        result = await asyncio.to_thread(_read)
        if result is None:
            raise HTTPException(404, f"File not found: {path}")
        await _assert_path_readable(
            db, user.entity_id, rel, user, credential=credential,
        )
        return result


@router.get("/info")
async def file_info(
    user: User = Depends(get_current_user),
    path: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Get file/directory metadata."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    full = _resolve(user.entity_id, path)
    root = _entity_root(user.entity_id)
    def _info():
        if not os.path.exists(full):
            return None
        return _file_info(full, root)

    async with _entity_filesystem_read_boundary(root):
        if os.path.exists(full):
            rel = normalize_rel_path(os.path.relpath(full, root))
            _assert_user_visible_rel(rel, is_dir=os.path.isdir(full), action="inspect")
            await _assert_path_readable(
                db,
                user.entity_id,
                rel,
                user,
                is_dir=os.path.isdir(full),
                credential=credential,
            )
        result = await asyncio.to_thread(_info)
        if result is None:
            raise HTTPException(404, f"Not found: {path}")
        await _assert_path_readable(
            db,
            user.entity_id,
            normalize_rel_path(os.path.relpath(full, root)),
            user,
            is_dir=os.path.isdir(full),
            credential=credential,
        )
        return result


@router.post("/write")
async def write_file(
    req: WriteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or update a text file."""
    _require_fs_ready_for_mutation()
    full = _resolve(user.entity_id, req.path)
    root = _entity_root(user.entity_id)
    rel = normalize_rel_path(os.path.relpath(full, root))
    _assert_user_visible_rel(rel, is_dir=False, action="write")
    content_bytes = req.content.encode("utf-8")

    async def _persist_write_and_sync():
        staging_dir: str | None = None
        backup_path: str | None = None
        if os.path.exists(full):
            staging_root = os.path.join(root, ".trash", "filesystem-write")
            os.makedirs(staging_root, exist_ok=True)
            staging_dir = tempfile.mkdtemp(prefix="write-", dir=staging_root)
            backup_path = os.path.join(staging_dir, os.path.basename(full))
            await asyncio.to_thread(os.replace, full, backup_path)

        def _write():
            try:
                return write_entity_file_atomic(
                    user.entity_id,
                    rel,
                    content_bytes,
                    expected_size=len(content_bytes),
                    allow_empty=True,
                    lock_mode=EntityFileWriteLockMode.ALREADY_HELD,
                )
            except Exception:
                if backup_path is not None:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    os.replace(backup_path, full)
                if staging_dir is not None:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                raise

        written_path = await asyncio.to_thread(_write)
        try:
            from packages.core.services.knowledge_sync import sync_file_to_knowledge
            sync = await sync_file_to_knowledge(
                entity_id=user.entity_id,
                abs_path=written_path,
                entity_root=_entity_root(user.entity_id),
                source="manual",
                created_by=user.id,
                user_id=user.id,
                force=True,
                db=db,
                commit=True,
            )
            if not sync.synced:
                raise RuntimeError(sync.reason or "Knowledge projection was not created")
        except Exception as exc:
            try:
                if os.path.exists(written_path):
                    await asyncio.to_thread(os.remove, written_path)
                if backup_path is not None:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    await asyncio.to_thread(os.replace, backup_path, full)
                if staging_dir is not None:
                    await asyncio.to_thread(shutil.rmtree, staging_dir, True)
            except Exception:
                logger.critical("filesystem write rollback failed", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Write could not be synchronized with Knowledge",
            ) from exc
        if staging_dir is not None:
            await asyncio.to_thread(shutil.rmtree, staging_dir, True)
        append_log(user.entity_id, "WRITE", f"{user.email} wrote {req.path}")
        return sync

    async with _entity_filesystem_mutation_boundary(root):
        await _require_path_write_access(
            db,
            user=user,
            rel_path=rel,
            exists=os.path.exists(full),
        )
        if os.path.isdir(full):
            raise HTTPException(
                status_code=409,
                detail="A directory already exists at this path",
            )
        lock_handles = await _acquire_path_mutation_locks(root, rel)
        lock_handle = lock_handles[0]
        try:
            async def _write_and_sync():
                claim_result = None
                if req.save_session_id is not None and req.save_sequence is not None:
                    claim_result = await asyncio.to_thread(
                        claim_editor_write_intent,
                        lock_handle,
                        req.save_session_id,
                        req.save_sequence,
                        req.content,
                    )
                    if claim_result.status is EditorWriteIntentStatus.STALE:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "stale_write_intent",
                                "message": "A newer save already exists for this file",
                            },
                        )
                    if claim_result.status is EditorWriteIntentStatus.REPLAYED:
                        return claim_result.receipt or {}
                sync_result = await _persist_write_and_sync()
                receipt = {
                    "document_id": sync_result.document_id,
                    "reason": sync_result.reason,
                }
                if req.save_session_id is not None and req.save_sequence is not None:
                    await asyncio.to_thread(
                        mark_editor_write_intent_committed,
                        lock_handle,
                        req.save_session_id,
                        req.save_sequence,
                        req.content,
                        receipt=receipt,
                    )
                return receipt

            sync_receipt = await _finish_filesystem_mutation(_write_and_sync())
        finally:
            _release_path_mutation_locks(lock_handles)
    return {
        "status": "ok",
        "path": req.path,
        "size": len(req.content.encode("utf-8")),
        "knowledge_sync": {
            "synced": True,
            "document_id": sync_receipt.get("document_id"),
            "reason": sync_receipt.get("reason"),
        },
    }


@router.post("/mkdir")
async def make_directory(
    req: MkdirRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a directory (and parents)."""
    _require_fs_ready_for_mutation()
    full = _resolve(user.entity_id, req.path)
    root = _entity_root(user.entity_id)
    rel = normalize_rel_path(os.path.relpath(full, root))
    _assert_user_visible_rel(rel, is_dir=True, action="create")

    async with _entity_filesystem_mutation_boundary(root):
        lock_handles = await _acquire_path_mutation_locks(root, rel)
        try:
            existed = os.path.isdir(full)
            await _require_directory_write_access(
                db,
                user=user,
                rel_path=rel if existed else os.path.dirname(rel),
            )

            async def _mkdir_and_sync():
                await asyncio.to_thread(os.makedirs, full, exist_ok=True)
                try:
                    from packages.core.services.knowledge_sync import ensure_folder_path
                    folder_id = await ensure_folder_path(
                        user.entity_id,
                        os.path.relpath(full, _entity_root(user.entity_id)),
                        owner_id=user.id,
                        db=db,
                    )
                    if not folder_id:
                        raise RuntimeError("Knowledge folder projection was not created")
                    await db.commit()
                except Exception as exc:
                    if not existed:
                        await asyncio.to_thread(shutil.rmtree, full, True)
                    raise HTTPException(
                        status_code=500,
                        detail="Directory could not be synchronized with Knowledge",
                    ) from exc

            await _finish_filesystem_mutation(_mkdir_and_sync())
        finally:
            _release_path_mutation_locks(lock_handles)
    return {"status": "ok", "path": req.path}


@router.post("/move")
async def move_file(
    req: MoveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Move or rename a file/directory."""
    _require_fs_ready_for_mutation()
    src = _resolve(user.entity_id, req.src)
    dest = _resolve(user.entity_id, req.dest)
    root = _entity_root(user.entity_id)
    src_rel = normalize_rel_path(os.path.relpath(src, root))
    dest_rel = normalize_rel_path(os.path.relpath(dest, root))

    async with _entity_filesystem_mutation_boundary(root):
        lock_handles = await _acquire_path_mutation_locks(root, src_rel, dest_rel)
        try:
            if not os.path.exists(src):
                raise HTTPException(404, f"Source not found: {req.src}")
            src_is_dir = os.path.isdir(src)
            _assert_user_visible_rel(src_rel, is_dir=src_is_dir, action="move")
            _assert_user_visible_rel(dest_rel, is_dir=src_is_dir, action="move to")
            source_documents = await _require_path_mutation_access(
                db,
                user=user,
                rel_path=src_rel,
                full_path=src,
                action="move",
                destination_rel_path=dest_rel,
            )
            if src == dest:
                return {"status": "ok", "src": req.src, "dest": req.dest}
            if os.path.exists(dest):
                raise HTTPException(
                    status_code=409,
                    detail="Move destination already exists; delete it explicitly before replacing it",
                )
            if (
                not await user_is_effective_entity_admin(db, user)
                and os.path.dirname(src_rel) != os.path.dirname(dest_rel)
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Move files through Knowledge when changing their parent folder",
                )

            def _move():
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(src, dest)
                return True

            async def _move_and_sync():
                await asyncio.to_thread(_move)
                try:
                    from packages.core.services.knowledge_sync import move_path
                    changed = await move_path(
                        user.entity_id,
                        src_rel,
                        dest_rel,
                        db=db,
                        commit=True,
                    )
                    if source_documents and not changed:
                        raise RuntimeError("Knowledge projection did not move")
                except Exception as exc:
                    try:
                        os.makedirs(os.path.dirname(src), exist_ok=True)
                        await asyncio.to_thread(os.replace, dest, src)
                    except Exception:
                        logger.critical("filesystem move rollback failed", exc_info=True)
                    raise HTTPException(
                        status_code=500,
                        detail="Move could not be synchronized with Knowledge",
                    ) from exc
                append_log(user.entity_id, "MOVE", f"{user.email} moved {req.src} → {req.dest}")

            await _finish_filesystem_mutation(_move_and_sync())
        finally:
            _release_path_mutation_locks(lock_handles)

    return {"status": "ok", "src": req.src, "dest": req.dest}



@router.post("/delete")
async def delete_file(
    req: DeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a file or directory. System files are protected."""
    _require_fs_ready_for_mutation()
    basename = os.path.basename(req.path)
    if basename in SYSTEM_FILES or basename in SYSTEM_DIRS:
        raise HTTPException(403, f"Cannot delete system file: {basename}")
    full = _resolve(user.entity_id, req.path)
    root = _entity_root(user.entity_id)
    rel = normalize_rel_path(os.path.relpath(full, root))

    async with _entity_filesystem_mutation_boundary(root):
        lock_handles = await _acquire_path_mutation_locks(root, rel)
        try:
            if not os.path.exists(full):
                raise HTTPException(404, f"Not found: {req.path}")
            _assert_user_visible_rel(rel, is_dir=os.path.isdir(full), action="delete")
            source_documents = await _require_path_mutation_access(
                db,
                user=user,
                rel_path=rel,
                full_path=full,
                action="delete",
            )

            async def _delete_and_sync():
                staging_root = os.path.join(root, ".trash", "filesystem-delete")
                os.makedirs(staging_root, exist_ok=True)
                staging_dir = tempfile.mkdtemp(prefix="delete-", dir=staging_root)
                staged = os.path.join(staging_dir, os.path.basename(full))
                await asyncio.to_thread(os.replace, full, staged)
                try:
                    from packages.core.services.knowledge_sync import trash_path
                    changed = await trash_path(
                        user.entity_id,
                        rel,
                        is_directory=os.path.isdir(staged),
                        db=db,
                        commit=True,
                    )
                    if source_documents and not changed:
                        raise RuntimeError("Knowledge projection did not move to Trash")
                except Exception as exc:
                    try:
                        os.makedirs(os.path.dirname(full), exist_ok=True)
                        await asyncio.to_thread(os.replace, staged, full)
                        await asyncio.to_thread(shutil.rmtree, staging_dir, True)
                    except Exception:
                        logger.critical("filesystem delete rollback failed", exc_info=True)
                    raise HTTPException(
                        status_code=500,
                        detail="Delete could not be synchronized with Knowledge",
                    ) from exc

                await asyncio.to_thread(shutil.rmtree, staging_dir, True)
                append_log(user.entity_id, "DELETE", f"{user.email} deleted {req.path}")

            await _finish_filesystem_mutation(_delete_and_sync())
        finally:
            _release_path_mutation_locks(lock_handles)

    return {"status": "ok", "path": req.path}



@router.post("/upload")
async def upload_file(
    user: User = Depends(get_current_user),
    path: str = Query(".", description="Target directory path"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a binary file to the entity filesystem."""
    _require_fs_ready_for_mutation()
    target_dir = _resolve(user.entity_id, path)
    root = _entity_root(user.entity_id)
    target_rel = normalize_rel_path(os.path.relpath(target_dir, root))
    _assert_user_visible_rel(target_rel, is_dir=True, action="upload to")
    await _require_directory_write_access(
        db,
        user=user,
        rel_path=target_rel,
    )
    filename = os.path.basename(file.filename or "upload")
    if not filename:
        raise HTTPException(400, "Invalid filename")
    candidate_rel = normalize_rel_path(os.path.join(target_rel, filename))
    _assert_user_visible_rel(candidate_rel, is_dir=False, action="upload")

    def _prepare_rel():
        t = os.path.join(target_dir, filename)
        if os.path.exists(t):
            base_name, ext = os.path.splitext(filename)
            t = os.path.join(target_dir, f"{base_name}_{int(datetime.now().timestamp())}{ext}")
        return normalize_rel_path(os.path.relpath(t, root))

    fd, tmp_path = tempfile.mkstemp(prefix="manor-upload-", suffix=".tmp")
    os.close(fd)
    total = 0
    try:
        import aiofiles

        async with aiofiles.open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 256):
                total += len(chunk)
                await f.write(chunk)

        from packages.core.services.upload_security import (
            UploadSecurityError,
            inspect_upload_path,
        )
        try:
            await inspect_upload_path(
                tmp_path,
                filename=filename,
                declared_content_type=file.content_type,
            )
        except UploadSecurityError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

        async with _entity_filesystem_mutation_boundary(root):
            # Serialize destination selection with writes and moves so a
            # collision cannot change between naming and persistence.
            rel_target = await asyncio.to_thread(_prepare_rel)

            def _persist_upload():
                target = copy_entity_file_atomic(
                    user.entity_id,
                    rel_target,
                    tmp_path,
                    expected_size=total,
                    allow_empty=True,
                    lock_mode=EntityFileWriteLockMode.ALREADY_HELD,
                )
                rel = normalize_rel_path(os.path.relpath(target, root))
                return target, rel

            lock_handles = await _acquire_path_mutation_locks(root, rel_target)
            try:
                await _require_directory_write_access(
                    db,
                    user=user,
                    rel_path=target_rel,
                )
                if os.path.exists(_resolve(user.entity_id, rel_target)):
                    raise HTTPException(
                        status_code=409,
                        detail="Upload destination changed; retry the upload",
                    )

                async def _upload_and_sync():
                    target, rel_path = await asyncio.to_thread(_persist_upload)
                    try:
                        from packages.core.services.knowledge_sync import sync_file_to_knowledge
                        sync = await sync_file_to_knowledge(
                            entity_id=user.entity_id,
                            abs_path=target,
                            entity_root=_entity_root(user.entity_id),
                            source="upload",
                            created_by=user.id,
                            user_id=user.id,
                            force=True,
                            db=db,
                            commit=True,
                        )
                        if not sync.synced:
                            raise RuntimeError(sync.reason or "Knowledge projection was not created")
                    except Exception as exc:
                        try:
                            await asyncio.to_thread(os.remove, target)
                        except OSError:
                            logger.critical("filesystem upload rollback failed", exc_info=True)
                        raise HTTPException(
                            status_code=500,
                            detail="Upload could not be synchronized with Knowledge",
                        ) from exc
                    append_log(user.entity_id, "UPLOAD", f"{user.email} uploaded {rel_path}")
                    return target, rel_path, sync

                target, rel_path, sync = await _finish_filesystem_mutation(
                    _upload_and_sync(),
                )
            finally:
                _release_path_mutation_locks(lock_handles)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return {
        "status": "ok",
        "path": rel_path,
        "filename": os.path.basename(target),
        "size": total,
        "knowledge_sync": {
            "synced": True,
            "document_id": sync.document_id,
            "reason": sync.reason,
        },
    }


@router.get("/search")
async def search_files(
    user: User = Depends(get_current_user),
    query: str = Query(..., description="Search text (ripgrep)"),
    glob_pattern: str = Query("*.md", alias="glob", description="File pattern to search"),
    max_results: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Search file contents using ripgrep."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    root = _entity_root(user.entity_id)
    async with _entity_filesystem_read_boundary(root):
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg", "--json", "--max-count", "3", "--glob", glob_pattern,
                "--max-filesize", "1M", query, root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            return {"results": [], "error": "Search timed out"}
        except FileNotFoundError:
            return {"results": [], "error": "ripgrep not installed"}

        results = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                msg = json_mod.loads(line)
                if msg.get("type") == "match":
                    data = msg["data"]
                    rel_path = os.path.relpath(data["path"]["text"], root)
                    # Search results are user-facing; do not leak hidden/runtime files.
                    if not is_user_visible_path(rel_path):
                        continue
                    results.append({
                        "path": rel_path,
                        "line": data["line_number"],
                        "text": data["lines"]["text"].strip()[:200],
                    })
            except Exception:
                continue
            if len(results) >= max_results:
                break

        # Drop hits inside Knowledge documents the caller cannot read — otherwise
        # ripgrep would leak private-document content snippets across the entity.
        blocked = await _unreadable_doc_paths(
            db, user.entity_id, [r["path"] for r in results], user,
            credential=credential,
        )
        if blocked:
            results = [r for r in results if normalize_rel_path(r["path"]) not in blocked]

        return {"results": results, "query": query, "count": len(results)}


@router.get("/wiki-links")
async def resolve_wiki_links(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    path: str = Query(..., description="Path to .md file"),
):
    """Resolve [[wiki links]] in a markdown file to actual paths."""
    credential = AuthenticatedUserCredential.from_user(user)
    _require_fs()
    root = _entity_root(user.entity_id)
    full = _resolve(user.entity_id, path)
    _assert_user_visible_rel(path, is_dir=False, action="read")
    async with _entity_filesystem_read_boundary(root):
        await _assert_path_readable(
            db, user.entity_id, normalize_rel_path(path), user,
            credential=credential,
        )

        from packages.core.models.document import Document
        from packages.core.services.document_service import get_document_content
        from packages.core.services.wiki_service import (
            build_file_index,
            extract_wiki_links,
            list_wiki_pages,
            resolve_link,
        )

        content: str | None = None
        if os.path.isfile(full):
            def _read_source_file():
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()

            content = await asyncio.to_thread(_read_source_file)
        else:
            doc = (await db.execute(
                select(Document).where(
                    Document.entity_id == user.entity_id,
                    Document.fs_path == path,
                    Document.is_trashed.is_(False),
                ).limit(1)
            )).scalar_one_or_none()
            if doc:
                content = await get_document_content(db, doc.id, user.entity_id)
        if content is None:
            raise HTTPException(404, f"File not found: {path}")

        candidate_paths = await asyncio.to_thread(list_wiki_pages, user.entity_id)
        blocked_candidate_paths = await _unreadable_doc_paths(
            db,
            user.entity_id,
            candidate_paths,
            user,
            credential=credential,
        )
        allowed_paths = {
            normalize_rel_path(candidate_path)
            for candidate_path in candidate_paths
            if normalize_rel_path(candidate_path) not in blocked_candidate_paths
        }

        def _resolve_links():
            links = extract_wiki_links(content)
            file_index = build_file_index(
                user.entity_id,
                allowed_paths=allowed_paths,
            )
            resolved = []
            for target, display in links:
                resolved_path = resolve_link(
                    target,
                    user.entity_id,
                    file_index,
                    allowed_paths=allowed_paths,
                )
                resolved.append({
                    "target": target,
                    "display": display,
                    "resolved_path": resolved_path,
                    "exists": resolved_path is not None,
                })
            return resolved

        resolved = await asyncio.to_thread(_resolve_links)
        resolved_paths = sorted({
            item["resolved_path"]
            for item in resolved
            if item.get("resolved_path")
        })
        blocked_paths = await _unreadable_doc_paths(
            db,
            user.entity_id,
            resolved_paths,
            user,
            credential=credential,
        )
        if blocked_paths:
            for item in resolved:
                if normalize_rel_path(item.get("resolved_path") or "") in blocked_paths:
                    item["resolved_path"] = None
                    item["exists"] = False
            resolved_paths = [
                path
                for path in resolved_paths
                if normalize_rel_path(path) not in blocked_paths
            ]
        docs_by_path: dict[str, Any] = {}
        if resolved_paths:
            docs = (await db.execute(
                select(Document).where(
                    Document.entity_id == user.entity_id,
                    Document.fs_path.in_(resolved_paths),
                    Document.is_trashed.is_(False),
                )
            )).scalars().all()
            docs_by_path = {doc.fs_path: doc for doc in docs if doc.fs_path}

        for item in resolved:
            doc = docs_by_path.get(item.get("resolved_path"))
            item["document_id"] = doc.id if doc else None
            item["document_name"] = doc.name if doc else None
            item["file_type"] = doc.file_type if doc else None
            item["vector_status"] = doc.vector_status if doc else None
        await _assert_path_readable(
            db,
            user.entity_id,
            normalize_rel_path(path),
            user,
            credential=credential,
        )
        return {"links": resolved, "file": path, "count": len(resolved)}


def _looks_like_markdown_doc(*, name: str | None, file_type: str | None, mime_type: str | None = None, fs_path: str | None = None) -> bool:
    ext_source = (fs_path or name or "").lower()
    normalized_type = (file_type or "").lower().lstrip(".")
    normalized_mime = (mime_type or "").lower()
    return (
        normalized_type in {"md", "markdown"}
        or normalized_mime == "text/markdown"
        or ext_source.endswith(".md")
        or ext_source.endswith(".markdown")
    )


def _document_wiki_path(doc: Any) -> str | None:
    fs_path = normalize_rel_path(getattr(doc, "fs_path", None) or "")
    if fs_path:
        return fs_path if is_user_visible_path(fs_path) else None

    raw_name = normalize_rel_path(getattr(doc, "name", None) or "")
    filename = os.path.basename(raw_name) or f"{getattr(doc, 'id', 'document')}.md"
    if not filename.lower().endswith((".md", ".markdown")):
        filename = f"{filename}.md"
    if not is_user_visible_path(filename):
        filename = f"{getattr(doc, 'id', 'document')}.md"
    return normalize_rel_path(f"db-docs/{getattr(doc, 'id', 'document')}/{filename}")


def _document_inline_markdown(doc: Any) -> str:
    meta = getattr(doc, "metadata_", None)
    if not isinstance(meta, dict):
        return ""
    for key in ("content", "content_text"):
        value = meta.get(key)
        if isinstance(value, str):
            return value
    return ""


def _wiki_index_keys(*values: str | None) -> list[str]:
    keys: list[str] = []
    for value in values:
        raw = normalize_rel_path(str(value or "").strip())
        if not raw:
            continue
        variants = {raw, raw[:-3] if raw.lower().endswith(".md") else raw}
        basename = os.path.basename(raw)
        if basename:
            variants.add(basename)
            root, ext = os.path.splitext(basename)
            if ext:
                variants.add(root)
        for variant in variants:
            key = variant.strip().lower()
            if key and key not in keys:
                keys.append(key)
    return keys


def _merge_document_markdown_pages(graph: dict[str, Any], docs: list[Any]) -> None:
    """Merge DB-backed markdown documents into the user-visible wiki graph."""
    from packages.core.services.wiki_service import extract_wiki_links

    raw_pages = graph.get("pages") if isinstance(graph, dict) else []
    page_rows = [page for page in raw_pages if isinstance(page, dict)]
    pages_by_path: dict[str, dict[str, Any]] = {
        normalize_rel_path(str(page.get("path") or "")): page
        for page in page_rows
        if page.get("path")
    }

    for doc in docs:
        if not _looks_like_markdown_doc(
            name=getattr(doc, "name", None),
            file_type=getattr(doc, "file_type", None),
            mime_type=getattr(doc, "mime_type", None),
            fs_path=getattr(doc, "fs_path", None),
        ):
            continue
        path = _document_wiki_path(doc)
        if not path:
            continue
        title = os.path.splitext(os.path.basename(getattr(doc, "name", None) or path))[0] or path
        page = pages_by_path.get(path)
        if page is None:
            page = {
                "path": path,
                "title": title,
                "links": [],
                "backlinks": [],
                "_inline_markdown": _document_inline_markdown(doc),
            }
            pages_by_path[path] = page
        else:
            page.setdefault("_inline_markdown", _document_inline_markdown(doc))
        page["document_id"] = getattr(doc, "id", None)
        page["document_name"] = getattr(doc, "name", None)
        page["file_type"] = getattr(doc, "file_type", None)
        page["vector_status"] = getattr(doc, "vector_status", None)

    page_index: dict[str, str] = {}
    for path, page in pages_by_path.items():
        for key in _wiki_index_keys(path, page.get("title"), page.get("document_name")):
            page_index.setdefault(key, path)

    def resolve_target(target: str | None) -> str | None:
        for key in _wiki_index_keys(target):
            resolved = page_index.get(key)
            if resolved:
                return resolved
        return None

    for path, page in pages_by_path.items():
        inline_markdown = page.pop("_inline_markdown", "")
        if inline_markdown:
            page["links"] = []
            for target, display in extract_wiki_links(inline_markdown):
                resolved_path = resolve_target(target)
                page["links"].append({
                    "target": target,
                    "display": display,
                    "resolved_path": resolved_path,
                    "exists": resolved_path is not None,
                })
        else:
            links = page.get("links") if isinstance(page.get("links"), list) else []
            for link in links:
                if not isinstance(link, dict):
                    continue
                if not link.get("resolved_path"):
                    link["resolved_path"] = resolve_target(link.get("target"))
                link["exists"] = bool(link.get("resolved_path") in pages_by_path)
            page["links"] = links
        page["backlinks"] = []

    missing_by_target: dict[str, dict[str, Any]] = {}
    link_count = 0
    for path, page in pages_by_path.items():
        for link in page.get("links", []):
            if not isinstance(link, dict):
                continue
            link_count += 1
            resolved_path = normalize_rel_path(str(link.get("resolved_path") or ""))
            target_page = pages_by_path.get(resolved_path)
            if target_page:
                link["exists"] = True
                link["resolved_path"] = resolved_path
                link["document_id"] = target_page.get("document_id")
                link["document_name"] = target_page.get("document_name")
                backlink = {"source_path": path, "source_title": page.get("title")}
                backlinks = target_page.setdefault("backlinks", [])
                if backlink not in backlinks:
                    backlinks.append(backlink)
            else:
                link["exists"] = False
                target = str(link.get("target") or "").strip()
                if target:
                    row = missing_by_target.setdefault(target, {"target": target, "count": 0, "sources": []})
                    row["count"] = int(row["count"]) + 1
                    row["sources"].append({"path": path, "title": page.get("title")})

    orphaned_pages = [
        path
        for path, page in pages_by_path.items()
        if not page.get("backlinks") and os.path.basename(path) not in ("MANOR.md", "index.md", "log.md")
    ]

    graph["pages"] = sorted(pages_by_path.values(), key=lambda page: str(page.get("title") or "").lower())
    graph["missing_links"] = sorted(missing_by_target.values(), key=lambda row: (-int(row["count"]), str(row["target"]).lower()))
    graph["orphaned_pages"] = sorted(orphaned_pages)
    graph["page_count"] = len(pages_by_path)
    graph["link_count"] = link_count
    graph["missing_count"] = len(missing_by_target)
    graph["orphaned_count"] = len(orphaned_pages)


@router.get("/wiki-index")
async def wiki_index(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    net_id: str | None = Query(None, description="Optional Knowledge Net id. Alias for group_id."),
    group_id: str | None = Query(None, description="Optional DocumentGroup/Knowledge Net id."),
    workspace_id: str | None = Query(None, description="Optional workspace scope; uses that workspace's Knowledge Nets."),
):
    """Return the user-visible markdown wiki graph for navigation/search."""
    credential = AuthenticatedUserCredential.from_user(user)
    await require_workspace_readable(db, user, workspace_id)
    _require_fs()
    async with _entity_filesystem_read_boundary(_entity_root(user.entity_id)):
        return await _wiki_index_consistent(
            user=user,
            db=db,
            net_id=net_id,
            group_id=group_id,
            workspace_id=workspace_id,
            credential=credential,
        )


async def _wiki_index_consistent(
    *,
    user: User,
    db: AsyncSession,
    net_id: str | None,
    group_id: str | None,
    workspace_id: str | None,
    credential: AuthenticatedUserCredential | None = None,
):
    """Build the graph while its Document paths and physical files are stable."""

    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services.document_access import DocumentAccessContext
    from packages.core.services.wiki_service import build_wiki_graph

    credential = credential or AuthenticatedUserCredential.from_user(user)
    actor = await resolve_current_user_actor(db, credential)
    if actor is None:
        raise HTTPException(404, "Workspace not found")

    def _split_ids(value: str | None) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    requested_net_ids = list(dict.fromkeys([*_split_ids(net_id), *_split_ids(group_id)]))
    if workspace_id and await ResourcePermissionGate.authorize_workspace_read(
        db,
        credential=credential,
        workspace_id=workspace_id,
    ) is None:
        raise HTTPException(404, "Workspace not found")

    access_ctx = await DocumentAccessContext.load(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        role=user.role,
        _actor=actor,
    )
    allowed_paths: set[str] | None = None
    allowed_doc_ids: set[str] | None = None
    scope_group_ids: list[str] = []
    if requested_net_ids or workspace_id:
        group_stmt = select(DocumentGroup).where(DocumentGroup.entity_id == user.entity_id)
        if requested_net_ids:
            group_stmt = group_stmt.where(DocumentGroup.id.in_(requested_net_ids))
        if workspace_id:
            group_stmt = group_stmt.where(DocumentGroup.workspace_id == workspace_id)
        groups = (await db.execute(group_stmt)).scalars().all()
        for scoped_workspace_id in {
            str(group.workspace_id)
            for group in groups
            if group.workspace_id
        }:
            if await ResourcePermissionGate.authorize_workspace_read(
                db,
                credential=credential,
                workspace_id=scoped_workspace_id,
            ) is None:
                raise HTTPException(404, "Workspace not found")
        scope_group_ids = [
            group.id for group in groups
            if not (group.settings or {}).get("workspace_file_bucket")
        ]
        if requested_net_ids and len(scope_group_ids) != len(requested_net_ids):
            raise HTTPException(404, "Knowledge Net not found")
        if scope_group_ids:
            doc_rows = (await db.execute(
                select(Document.id, Document.fs_path, Document.name, Document.file_type, Document.mime_type)
                .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
                .where(
                    Document.entity_id == user.entity_id,
                    DocumentGroupMember.group_id.in_(scope_group_ids),
                    Document.is_trashed.is_(False),
                )
            )).all()
            allowed_doc_ids = {
                doc_id
                for doc_id, fs_path, name, file_type, mime_type in doc_rows
                if _looks_like_markdown_doc(name=name, file_type=file_type, mime_type=mime_type, fs_path=fs_path)
            }
            allowed_paths = {
                normalize_rel_path(path)
                for _doc_id, path, name, file_type, mime_type in doc_rows
                if path and _looks_like_markdown_doc(name=name, file_type=file_type, mime_type=mime_type, fs_path=path)
            }
        else:
            allowed_doc_ids = set()
            allowed_paths = set()

    graph = await asyncio.to_thread(build_wiki_graph, user.entity_id, allowed_paths=allowed_paths)
    # Drop pages backed by a Knowledge document the caller cannot read, so the
    # wiki graph never exposes a private document's name, links, or inline
    # markdown to a same-entity member.
    _graph_pages = graph.get("pages") if isinstance(graph, dict) else None
    if isinstance(_graph_pages, list) and _graph_pages:
        _page_paths = [
            p.get("path") for p in _graph_pages if isinstance(p, dict) and p.get("path")
        ]
        _blocked_pages = await _unreadable_doc_paths(
            db, user.entity_id, _page_paths, user, credential=credential,
        )
        if _blocked_pages:
            readable_page_paths = {
                normalize_rel_path(str(page.get("path") or ""))
                for page in _graph_pages
                if isinstance(page, dict)
                and normalize_rel_path(str(page.get("path") or "")) not in _blocked_pages
            }
            # Rebuild instead of removing only page rows: backlinks, missing
            # links, orphan paths, and counts are all derived from the page set.
            graph = await asyncio.to_thread(
                build_wiki_graph,
                user.entity_id,
                allowed_paths=readable_page_paths,
            )
    if requested_net_ids or workspace_id:
        graph["scope"] = {
            "kind": "knowledge_net" if requested_net_ids else "workspace",
            "net_ids": scope_group_ids,
            "workspace_id": workspace_id,
        }
    pages = graph.get("pages") if isinstance(graph, dict) else []
    page_rows = pages if isinstance(pages, list) else []
    paths = {
        page.get("path")
        for page in page_rows
        if isinstance(page, dict) and page.get("path")
    }
    for page in page_rows:
        if not isinstance(page, dict):
            continue
        for link in page.get("links", []):
            if isinstance(link, dict) and link.get("resolved_path"):
                paths.add(link["resolved_path"])

    docs_by_path: dict[str, Any] = {}
    if paths:
        docs = (await db.execute(
            select(Document).where(
                Document.entity_id == user.entity_id,
                Document.fs_path.in_(sorted(paths)),
                Document.is_trashed.is_(False),
            )
        )).scalars().all()
        await access_ctx.preload_documents(db, list(docs))
        docs = [
            doc
            for doc in docs
            if await access_ctx.can_read_document(db, doc)
        ]
        docs_by_path = {doc.fs_path: doc for doc in docs if doc.fs_path}

    for page in page_rows:
        if not isinstance(page, dict):
            continue
        doc = docs_by_path.get(page.get("path"))
        if doc:
            page["document_id"] = doc.id
            page["document_name"] = doc.name
            page["file_type"] = doc.file_type
            page["vector_status"] = doc.vector_status
        for link in page.get("links", []):
            if not isinstance(link, dict):
                continue
            link_doc = docs_by_path.get(link.get("resolved_path"))
            if link_doc:
                link["document_id"] = link_doc.id
                link["document_name"] = link_doc.name

    doc_stmt = select(Document).where(
        Document.entity_id == user.entity_id,
        Document.is_trashed.is_(False),
        or_(
            Document.file_type.in_(("md", "markdown")),
            Document.mime_type == "text/markdown",
            Document.name.ilike("%.md"),
            Document.name.ilike("%.markdown"),
            Document.fs_path.ilike("%.md"),
            Document.fs_path.ilike("%.markdown"),
        ),
    )
    if allowed_doc_ids is not None:
        if allowed_doc_ids:
            doc_stmt = doc_stmt.where(Document.id.in_(allowed_doc_ids))
        else:
            doc_stmt = None
    markdown_docs = (await db.execute(doc_stmt)).scalars().all() if doc_stmt is not None else []
    await access_ctx.preload_documents(db, list(markdown_docs))
    markdown_docs = [
        doc
        for doc in markdown_docs
        if await access_ctx.can_read_document(db, doc, allow_redacted=False)
    ]
    _merge_document_markdown_pages(graph, list(markdown_docs))

    if await resolve_current_user_actor(db, credential) != actor:
        raise HTTPException(404, "Workspace not found")

    return graph


@router.get("/lint")
async def lint_knowledge_base(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run knowledge base health check."""
    _require_fs()
    if not await user_is_effective_entity_admin(db, user):
        raise HTTPException(403, "Entity admin access required")
    from packages.core.services.wiki_service import lint_entity
    async with _entity_filesystem_read_boundary(_entity_root(user.entity_id)):
        result = await asyncio.to_thread(lint_entity, user.entity_id)
        return {
            "entity_id": user.entity_id,
            "broken_links": result["broken_links"][:20],
            "broken_links_count": len(result["broken_links"]),
            "orphaned_pages": result["orphaned_pages"][:20],
            "orphaned_pages_count": len(result["orphaned_pages"]),
            "unprocessed_files": result["unprocessed_files"][:20],
            "unprocessed_files_count": len(result["unprocessed_files"]),
        }


# ── Raw file serving (avatars, uploads, etc.) ──
# MUST be last — catch-all route pattern

def _resolve_signed_entity_file(token: str) -> tuple[_Path, str, int]:
    """Validate a signed public-file token and resolve it to a local file."""
    _require_fs()

    payload = verify_file_access_token(token)
    if not payload:
        raise HTTPException(403, "Invalid or expired file token")

    entity_id = payload["entity_id"]
    path = payload["path"]
    entity_root = _Path(get_entity_root(entity_id)).resolve()
    file_path = (entity_root / path).resolve()

    try:
        file_path.relative_to(entity_root)
    except ValueError:
        raise HTTPException(403, "Access denied")
    if not file_path.is_file():
        raise _file_not_found()

    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return file_path, content_type, file_path.stat().st_size


async def _assert_signed_entity_file_readable(
    token: str,
    db: AsyncSession,
) -> None:
    """Revalidate actor-bound download tokens against current ACL/state."""
    payload = verify_file_access_token(token)
    if not payload:
        raise HTTPException(403, "Invalid or expired file token")
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        # Legacy provider/media tokens remain exact-path, short-lived bearer
        # tokens. Local Knowledge exports always carry an actor claim.
        return
    from packages.core.services.document_access import unreadable_document_paths

    blocked = await unreadable_document_paths(
        db,
        entity_id=str(payload["entity_id"]),
        rel_paths=[str(payload["path"])],
        user_id=user_id,
        actor_type="agent",
    )
    if blocked:
        raise _file_not_found()


@router.head("/public/{token}")
@router.head("/public/{token}/{filename:path}")
async def head_signed_entity_file(
    token: str,
    filename: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Allow media providers to preflight signed image URLs before GET."""
    file_path, content_type, file_size = _resolve_signed_entity_file(token)
    await _assert_signed_entity_file_readable(token, db)
    response_type, headers = _signed_file_response_metadata(file_path, content_type)
    headers["Content-Length"] = str(file_size)
    return RawResponse(
        status_code=200,
        media_type=response_type,
        headers=headers,
    )


@router.get("/public/{token}")
@router.get("/public/{token}/{filename:path}")
async def serve_signed_entity_file(
    token: str,
    filename: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Serve a short-lived signed file URL for external media providers."""
    file_path, content_type, _file_size = _resolve_signed_entity_file(token)
    await _assert_signed_entity_file_readable(token, db)
    response_type, headers = _signed_file_response_metadata(file_path, content_type)
    return FileResponse(
        path=str(file_path),
        media_type=response_type,
        headers=headers,
    )


@router.get("/{entity_id}/{path:path}")
async def serve_entity_file(
    entity_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve a raw file from the entity filesystem (avatars, Marketplace media).

    Avatars and explicitly published Marketplace media are public assets. All
    other entity files require bearer auth and must belong to the
    authenticated user's entity.

    Stale-URL cleanup: when an avatar path 404s, schedule a background task
    that clears any User / Staff row still pointing at the missing file.
    See :func:`_clear_stale_avatar_url` for rationale.
    """
    _require_fs()

    entity_root = _Path(get_entity_root(entity_id)).resolve()
    file_path = (entity_root / path).resolve()

    # Security: ensure path doesn't escape entity root.
    try:
        rel_path_obj = file_path.relative_to(entity_root)
    except ValueError:
        raise HTTPException(403, "Access denied")
    if not file_path.is_file():
        # Self-healing: if the missing path is an avatar, schedule a
        # fire-and-forget task to null out any User / Staff rows still
        # referencing it so we don't 404 on every future render. The
        # exact URL stored in `users.avatar_url` is the public path
        # under /api/v1/fs/, so reconstruct it before scheduling.
        #
        # We use ``asyncio.create_task`` rather than FastAPI's
        # ``BackgroundTasks`` because we're about to ``raise
        # HTTPException`` — and the exception handler bypasses the
        # request's BackgroundTasks lifecycle. The task is held in a
        # module-level set so it doesn't get garbage-collected before
        # the loop schedules it.
        rel_for_match = normalize_rel_path(str(rel_path_obj))
        if _is_public_raw_file_path(rel_for_match) and rel_for_match.startswith("avatars/"):
            stale_url = f"/api/v1/fs/{entity_id}/{rel_for_match}"
            task = asyncio.create_task(_clear_stale_avatar_url(stale_url))
            _PENDING_AVATAR_CLEANUP_TASKS.add(task)
            task.add_done_callback(_PENDING_AVATAR_CLEANUP_TASKS.discard)
        raise _file_not_found()
    rel_path = normalize_rel_path(str(rel_path_obj))
    is_public = _is_public_raw_file_path(rel_path)
    if not is_public:
        authenticated = await _optional_user_from_bearer(request, db)
        if not authenticated:
            raise HTTPException(401, "Not authenticated")
        user, credential = authenticated
        if user.entity_id != entity_id:
            raise HTTPException(403, "Access denied")
        if not is_user_visible_path(rel_path):
            raise HTTPException(403, "Access denied")
        read_boundary = _entity_filesystem_read_boundary(str(entity_root))
        await read_boundary.__aenter__()
        try:
            if not file_path.is_file():
                raise _file_not_found()
            # A Knowledge document served as raw bytes must still honor
            # Document.visibility — otherwise a same-entity member could fetch a
            # private file directly by URL.
            await _assert_path_readable(
                db,
                entity_id,
                rel_path,
                user,
                credential=credential,
            )
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            response_type, security_headers = _signed_file_response_metadata(file_path, content_type)
            security_headers["Cache-Control"] = "private, no-store"

            return _ReadLockedFileResponse(
                path=str(file_path),
                media_type=response_type,
                headers=security_headers,
                read_boundary=read_boundary,
            )
        except BaseException:
            await read_boundary.__aexit__(None, None, None)
            raise

    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    response_type, security_headers = _signed_file_response_metadata(file_path, content_type)
    security_headers["Cache-Control"] = "public, max-age=86400"
    return FileResponse(
        path=str(file_path),
        media_type=response_type,
        headers=security_headers,
    )
