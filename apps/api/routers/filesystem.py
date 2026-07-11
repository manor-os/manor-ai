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
import json as json_mod
import logging
import mimetypes
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response as RawResponse
from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.staff import Staff
from packages.core.models.user import User
from packages.core.services.entity_fs import (
    EntityFilesystemError,
    SYSTEM_DIRS,
    SYSTEM_FILES,
    assert_entity_filesystem_ready,
    copy_entity_file_atomic,
    get_entity_root,
    is_fs_enabled,
    is_system_path,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fs", tags=["filesystem"])


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


def _file_not_found() -> HTTPException:
    """Return an uncacheable 404 for file-serving misses."""
    return HTTPException(404, "File not found", headers={"Cache-Control": "no-store"})


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


_PUBLIC_RAW_PREFIXES: tuple[str, ...] = (
    "avatars/",
)


def _is_public_raw_file_path(rel_path: str) -> bool:
    rel = normalize_rel_path(rel_path)
    return any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in _PUBLIC_RAW_PREFIXES)


async def _optional_user_from_bearer(request: Request, db: AsyncSession) -> User | None:
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
    if not user or user.status != "active":
        return None
    return user


def _file_info(full_path: str, root: str) -> dict[str, Any]:
    """Build file metadata dict."""
    rel = os.path.relpath(full_path, root)
    name = os.path.basename(full_path)
    is_dir = os.path.isdir(full_path)
    try:
        stat = os.stat(full_path)
        item_count = None
        if is_dir:
            try:
                item_count = len([
                    e for e in os.listdir(full_path)
                    if _is_visible_browser_item(os.path.join(full_path, e), root)
                ])
            except OSError:
                item_count = 0
        return {
            "name": name,
            "path": rel,
            "type": "directory" if is_dir else "file",
            "size": stat.st_size if not is_dir else None,
            "item_count": item_count,
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
):
    """List contents of a directory."""
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

    items = await asyncio.to_thread(_list)
    if items is None:
        raise HTTPException(404, f"Directory not found: {path}")
    return {"items": items, "path": os.path.relpath(full, root), "count": len(items)}


@router.get("/tree")
async def directory_tree(
    user: User = Depends(get_current_user),
    max_depth: int = Query(3, ge=1, le=10),
    show_system: bool = Query(False),
):
    """Full directory tree for sidebar file browser."""
    _require_fs()
    root = _entity_root(user.entity_id)

    def walk(dir_path: str, depth: int) -> list[dict]:
        if depth > max_depth:
            return []
        items = []
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return []
        for name in entries:
            if _is_hidden(name, show_system):
                continue
            full = os.path.join(dir_path, name)
            if not _is_visible_browser_item(full, root, show_system=show_system):
                continue
            rel = os.path.relpath(full, root)
            is_dir = os.path.isdir(full)
            node: dict[str, Any] = {"name": name, "path": rel, "type": "directory" if is_dir else "file"}
            if is_dir:
                node["children"] = walk(full, depth + 1)
            else:
                node["extension"] = os.path.splitext(name)[1]
            items.append(node)
        items.sort(key=lambda x: (0 if x["type"] == "directory" else 1, x["name"].lower()))
        return items

    tree = await asyncio.to_thread(walk, root, 1)
    return {"tree": tree, "entity_id": user.entity_id}


@router.get("/read")
async def read_file(
    user: User = Depends(get_current_user),
    path: str = Query(..., description="Relative path to file"),
):
    """Read file content. Returns text for text files, base64 for binary."""
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

    result = await asyncio.to_thread(_read)
    if result is None:
        raise HTTPException(404, f"File not found: {path}")
    return result


@router.get("/info")
async def file_info(
    user: User = Depends(get_current_user),
    path: str = Query(...),
):
    """Get file/directory metadata."""
    _require_fs()
    full = _resolve(user.entity_id, path)
    root = _entity_root(user.entity_id)
    if os.path.exists(full):
        rel = normalize_rel_path(os.path.relpath(full, root))
        _assert_user_visible_rel(rel, is_dir=os.path.isdir(full), action="inspect")

    def _info():
        if not os.path.exists(full):
            return None
        return _file_info(full, root)

    result = await asyncio.to_thread(_info)
    if result is None:
        raise HTTPException(404, f"Not found: {path}")
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

    def _write():
        target = write_entity_file_atomic(
            user.entity_id,
            rel,
            content_bytes,
            expected_size=len(content_bytes),
            allow_empty=True,
        )
        append_log(user.entity_id, "WRITE", f"{user.email} wrote {req.path}")
        return target

    full = await asyncio.to_thread(_write)
    sync_info = {"synced": False, "reason": "not_synced"}
    try:
        from packages.core.services.knowledge_sync import sync_file_to_knowledge
        sync = await sync_file_to_knowledge(
            entity_id=user.entity_id,
            abs_path=full,
            entity_root=_entity_root(user.entity_id),
            source="manual",
            created_by=(user.display_name or user.email),
            force=True,
        )
        sync_info = {"synced": sync.synced, "document_id": sync.document_id, "reason": sync.reason}
    except Exception:
        logger.warning("filesystem write sync failed", exc_info=True)
    return {
        "status": "ok",
        "path": req.path,
        "size": len(req.content.encode("utf-8")),
        "knowledge_sync": sync_info,
    }


@router.post("/mkdir")
async def make_directory(
    req: MkdirRequest,
    user: User = Depends(get_current_user),
):
    """Create a directory (and parents)."""
    _require_fs_ready_for_mutation()
    full = _resolve(user.entity_id, req.path)
    root = _entity_root(user.entity_id)
    rel = normalize_rel_path(os.path.relpath(full, root))
    _assert_user_visible_rel(rel, is_dir=True, action="create")
    await asyncio.to_thread(os.makedirs, full, exist_ok=True)
    try:
        from packages.core.services.knowledge_sync import ensure_folder_path
        await ensure_folder_path(user.entity_id, os.path.relpath(full, _entity_root(user.entity_id)))
    except Exception:
        logger.warning("filesystem mkdir sync failed", exc_info=True)
    return {"status": "ok", "path": req.path}


@router.post("/move")
async def move_file(
    req: MoveRequest,
    user: User = Depends(get_current_user),
):
    """Move or rename a file/directory."""
    _require_fs_ready_for_mutation()
    src = _resolve(user.entity_id, req.src)
    dest = _resolve(user.entity_id, req.dest)
    if not os.path.exists(src):
        raise HTTPException(404, f"Source not found: {req.src}")
    src_is_dir = os.path.isdir(src)
    _assert_user_visible_rel(req.src, is_dir=src_is_dir, action="move")
    _assert_user_visible_rel(req.dest, is_dir=src_is_dir, action="move to")

    def _move():
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        append_log(user.entity_id, "MOVE", f"{user.email} moved {req.src} → {req.dest}")
        return True

    await asyncio.to_thread(_move)

    # Keep Knowledge projection in sync with filesystem moves/renames.
    try:
        from packages.core.services.knowledge_sync import move_path
        await move_path(user.entity_id, req.src, req.dest)
    except Exception:
        logger.warning("filesystem move sync failed", exc_info=True)

    return {"status": "ok", "src": req.src, "dest": req.dest}



@router.post("/delete")
async def delete_file(
    req: DeleteRequest,
    user: User = Depends(get_current_user),
):
    """Delete a file or directory. System files are protected."""
    _require_fs_ready_for_mutation()
    basename = os.path.basename(req.path)
    if basename in SYSTEM_FILES or basename in SYSTEM_DIRS:
        raise HTTPException(403, f"Cannot delete system file: {basename}")
    full = _resolve(user.entity_id, req.path)
    if not os.path.exists(full):
        raise HTTPException(404, f"Not found: {req.path}")
    _assert_user_visible_rel(req.path, is_dir=os.path.isdir(full), action="delete")

    def _delete():
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        append_log(user.entity_id, "DELETE", f"{user.email} deleted {req.path}")
        return True

    await asyncio.to_thread(_delete)

    # Keep Knowledge projection in sync with filesystem deletes.
    try:
        from packages.core.services.knowledge_sync import trash_path
        await trash_path(user.entity_id, req.path)
    except Exception:
        logger.warning("filesystem delete sync failed", exc_info=True)

    return {"status": "ok", "path": req.path}



@router.post("/upload")
async def upload_file(
    user: User = Depends(get_current_user),
    path: str = Query(".", description="Target directory path"),
    file: UploadFile = File(...),
):
    """Upload a binary file to the entity filesystem."""
    _require_fs_ready_for_mutation()
    target_dir = _resolve(user.entity_id, path)
    root = _entity_root(user.entity_id)
    target_rel = normalize_rel_path(os.path.relpath(target_dir, root))
    _assert_user_visible_rel(target_rel, is_dir=True, action="upload to")
    filename = os.path.basename(file.filename or "upload")
    if not filename:
        raise HTTPException(400, "Invalid filename")
    candidate_rel = normalize_rel_path(os.path.join(target_rel, filename))
    _assert_user_visible_rel(candidate_rel, is_dir=False, action="upload")

    def _prepare_rel():
        os.makedirs(target_dir, exist_ok=True)
        t = os.path.join(target_dir, filename)
        if os.path.exists(t):
            base_name, ext = os.path.splitext(filename)
            t = os.path.join(target_dir, f"{base_name}_{int(datetime.now().timestamp())}{ext}")
        return normalize_rel_path(os.path.relpath(t, root))

    rel_target = await asyncio.to_thread(_prepare_rel)
    fd, tmp_path = tempfile.mkstemp(prefix="manor-upload-", suffix=".tmp")
    os.close(fd)
    total = 0
    try:
        import aiofiles

        async with aiofiles.open(tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 256):
                total += len(chunk)
                await f.write(chunk)

        def _persist_upload():
            target = copy_entity_file_atomic(
                user.entity_id,
                rel_target,
                tmp_path,
                expected_size=total,
                allow_empty=True,
            )
            rel = normalize_rel_path(os.path.relpath(target, root))
            append_log(user.entity_id, "UPLOAD", f"{user.email} uploaded {rel}")
            return target, rel

        target, rel_path = await asyncio.to_thread(_persist_upload)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    sync_info = {"synced": False, "reason": "not_synced"}
    try:
        from packages.core.services.knowledge_sync import sync_file_to_knowledge
        sync = await sync_file_to_knowledge(
            entity_id=user.entity_id,
            abs_path=target,
            entity_root=_entity_root(user.entity_id),
            source="upload",
            created_by=(user.display_name or user.email),
            force=True,
        )
        sync_info = {"synced": sync.synced, "document_id": sync.document_id, "reason": sync.reason}
    except Exception:
        logger.warning("filesystem upload sync failed", exc_info=True)
    return {
        "status": "ok",
        "path": rel_path,
        "filename": os.path.basename(target),
        "size": total,
        "knowledge_sync": sync_info,
    }


@router.get("/search")
async def search_files(
    user: User = Depends(get_current_user),
    query: str = Query(..., description="Search text (ripgrep)"),
    glob_pattern: str = Query("*.md", alias="glob", description="File pattern to search"),
    max_results: int = Query(20, ge=1, le=100),
):
    """Search file contents using ripgrep."""
    _require_fs()
    root = _entity_root(user.entity_id)
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

    return {"results": results, "query": query, "count": len(results)}


@router.get("/wiki-links")
async def resolve_wiki_links(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    path: str = Query(..., description="Path to .md file"),
):
    """Resolve [[wiki links]] in a markdown file to actual paths."""
    _require_fs()
    full = _resolve(user.entity_id, path)
    _assert_user_visible_rel(path, is_dir=False, action="read")

    from packages.core.models.document import Document
    from packages.core.services.document_service import get_document_content
    from packages.core.services.wiki_service import extract_wiki_links, resolve_link, build_file_index

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

    def _resolve_links():
        links = extract_wiki_links(content)
        file_index = build_file_index(user.entity_id)
        resolved = []
        for target, display in links:
            resolved_path = resolve_link(target, user.entity_id, file_index)
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
    _require_fs()

    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.services.wiki_service import build_wiki_graph

    def _split_ids(value: str | None) -> list[str]:
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    requested_net_ids = list(dict.fromkeys([*_split_ids(net_id), *_split_ids(group_id)]))
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
    _merge_document_markdown_pages(graph, list(markdown_docs))

    return graph


@router.get("/lint")
async def lint_knowledge_base(user: User = Depends(get_current_user)):
    """Run knowledge base health check."""
    _require_fs()
    from packages.core.services.wiki_service import lint_entity
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


@router.head("/public/{token}")
@router.head("/public/{token}/{filename:path}")
async def head_signed_entity_file(token: str, filename: str | None = None):
    """Allow media providers to preflight signed image URLs before GET."""
    _file_path, content_type, file_size = _resolve_signed_entity_file(token)
    return RawResponse(
        status_code=200,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=900",
            "Content-Length": str(file_size),
        },
    )


@router.get("/public/{token}")
@router.get("/public/{token}/{filename:path}")
async def serve_signed_entity_file(token: str, filename: str | None = None):
    """Serve a short-lived signed file URL for external media providers."""
    file_path, content_type, _file_size = _resolve_signed_entity_file(token)
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=900"},
    )


@router.get("/{entity_id}/{path:path}")
async def serve_entity_file(
    entity_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve a raw file from the entity filesystem (avatars, public uploads).

    Avatars are public assets. All other entity files require bearer auth and
    must belong to the authenticated user's entity.

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
        user = await _optional_user_from_bearer(request, db)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if user.entity_id != entity_id:
            raise HTTPException(403, "Access denied")
        if not is_user_visible_path(rel_path):
            raise HTTPException(403, "Access denied")

    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        headers={
            "Cache-Control": (
                "public, max-age=86400" if is_public else "private, no-store"
            )
        },
    )
