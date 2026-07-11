"""Auto-register generated files from MCP tool outputs into the knowledge base.

After an MCP tool returns its result, this module scans the output for
file paths (on /mnt/manor) and remote URLs (images/videos from CDNs)
and creates Document records so generated assets are visible and
searchable in the Knowledge page.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Patterns we look for in tool output
_LOCAL_PATH_RE = re.compile(r"/mnt/manor/[^\s\"',}]+")
_FS_ROOT = "/mnt/manor/"
_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"',]+\.(?:png|jpg|jpeg|webp|gif|mp4|mp3|wav|pdf)", re.IGNORECASE
)

# Known CDN patterns from our MCP tools
_CDN_PATTERNS = [
    "replicate.delivery",
    "jimeng.jianying.com",
    "oaidalleapiprodscus.blob.core.windows.net",
    "cdn.openai.com",
]

# File type mapping
_EXT_TO_TYPE = {
    ".png": ("png", "image/png"),
    ".jpg": ("jpg", "image/jpeg"),
    ".jpeg": ("jpg", "image/jpeg"),
    ".webp": ("webp", "image/webp"),
    ".gif": ("gif", "image/gif"),
    ".mp4": ("mp4", "video/mp4"),
    ".mp3": ("mp3", "audio/mpeg"),
    ".wav": ("wav", "audio/wav"),
    ".pdf": ("pdf", "application/pdf"),
}


async def register_generated_files(
    tool_output: str,
    *,
    entity_id: str,
    user_id: str = "",
    source: str = "mcp",
    tool_args: dict | None = None,
    origin: dict | None = None,
) -> int:
    """Scan tool output for generated files and register them as documents.

    Returns the number of documents registered.
    """
    if not entity_id or not tool_output:
        return 0

    registered = 0
    context = _extract_naming_context(tool_output, tool_args=tool_args)
    origin = dict(origin or {})
    matched_artifact = False

    # 1. Local filesystem paths
    local_paths = _LOCAL_PATH_RE.findall(tool_output)
    for path_str in local_paths:
        path_str = path_str.rstrip("}")
        p = Path(path_str)
        if p.exists() and p.is_file():
            matched_artifact = True
            # Store path relative to entity root: /mnt/manor/{entity_id}/
            entity_root = f"{_FS_ROOT}{entity_id}/"
            if path_str.startswith(entity_root):
                rel_path = path_str[len(entity_root):]
            elif path_str.startswith(_FS_ROOT):
                rel_path = path_str[len(_FS_ROOT):]
            else:
                rel_path = path_str
            ok = await _register_local(
                p,
                rel_path=rel_path,
                entity_id=entity_id,
                user_id=user_id,
                source=source,
                origin=origin,
            )
            if ok:
                registered += 1

    # 2. Remote URLs that look like generated media files
    urls = _IMAGE_URL_RE.findall(tool_output)
    for index, url in enumerate(urls):
        # Skip URLs that are clearly not generated assets (e.g. API docs, icons)
        if any(skip in url for skip in ("favicon", "icon", "logo", "badge")):
            continue
        matched_artifact = True
        display_name = _friendly_remote_name(
            url,
            prompt=context.get("prompt"),
            title=context.get("title"),
            index=index,
            total=len(urls),
        )
        ok = await _register_url(
            url,
            entity_id=entity_id,
            user_id=user_id,
            source=source,
            display_name=display_name,
            origin=origin,
        )
        if ok:
            registered += 1

    if matched_artifact:
        await _refresh_workspace_file_cache(entity_id=entity_id, origin=origin)

    return registered


async def _refresh_workspace_file_cache(*, entity_id: str, origin: dict) -> None:
    workspace_id = str(origin.get("workspace_id") or "").strip() if isinstance(origin, dict) else ""
    if not entity_id or not workspace_id:
        return
    try:
        from sqlalchemy import select

        from packages.core.database import async_session
        from packages.core.models.workspace import Workspace
        from packages.core.services.workspace_state_files import refresh_workspace_state_files

        async with async_session() as db:
            workspace = (
                await db.execute(
                    select(Workspace).where(
                        Workspace.entity_id == entity_id,
                        Workspace.id == workspace_id,
                    )
                )
            ).scalar_one_or_none()
            if workspace:
                await refresh_workspace_state_files(db, workspace)
    except Exception:
        logger.debug("Failed to refresh workspace FILES.md for generated file", exc_info=True)


def _extract_naming_context(tool_output: str, *, tool_args: dict | None = None) -> dict[str, str]:
    """Pull human-readable naming hints from tool args and JSON output."""
    context: dict[str, str] = {}

    if isinstance(tool_args, dict):
        for key in ("filename_hint", "title", "name", "prompt", "description"):
            value = tool_args.get(key)
            if isinstance(value, str) and value.strip():
                if key == "filename_hint":
                    context.setdefault("title", value.strip())
                elif key in ("title", "name"):
                    context.setdefault("title", value.strip())
                else:
                    context.setdefault("prompt", value.strip())

    try:
        parsed = json.loads(tool_output)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        for key in ("filename", "filename_hint", "title", "name"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                context.setdefault("title", value.strip())
        for key in ("prompt", "description"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                context.setdefault("prompt", value.strip())

    return context


def _slugify_name(value: str, *, fallback: str = "generated-file", max_words: int = 7) -> str:
    cleaned = re.sub(r"['\"]+", "", value.lower())
    cleaned = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", cleaned)
    words = [w for w in cleaned.strip().split() if w][:max_words]
    return "-".join(words) or fallback


def _looks_generated_name(name: str) -> bool:
    stem = Path(name).stem
    return bool(
        re.match(r"^gen[_-][a-z0-9]+(?:[_-]\d+)?$", stem, re.I)
        or re.match(r"^generated[_-]?(file|image|asset)(?:[_-]\d+)?$", stem, re.I)
        or re.match(r"^[a-f0-9]{24,}$", stem, re.I)
        or re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", stem, re.I)
    )


def _friendly_remote_name(
    url: str,
    *,
    prompt: str | None = None,
    title: str | None = None,
    index: int = 0,
    total: int = 1,
) -> str:
    from urllib.parse import urlparse, unquote

    parsed = urlparse(url)
    raw_name = unquote(parsed.path.split("/")[-1]) or "generated_file"
    raw_name = raw_name.split("?")[0]
    ext = Path(raw_name).suffix.lower()
    if not ext:
        ext = ".png"

    if raw_name and not _looks_generated_name(raw_name):
        return raw_name

    label = title or prompt or "generated image"
    stem = _slugify_name(label, fallback="generated-image")
    if total > 1:
        stem = f"{stem}-{index + 1}"
    return f"{stem}{ext}"


async def _register_local(
    path: Path,
    *,
    rel_path: str,
    entity_id: str,
    user_id: str,
    source: str,
    origin: dict,
) -> bool:
    """Register a local file as a document."""
    try:
        from packages.core.database import async_session
        from packages.core.models.document import Document
        from packages.core.services.document_metadata import merge_document_metadata
        from packages.core.services.document_service import upsert_document_by_fs_path
        from packages.core.services.knowledge_sync import ensure_folder_path
        from sqlalchemy import select

        ext = path.suffix.lower()
        file_type, mime_type = _EXT_TO_TYPE.get(ext, ("", "application/octet-stream"))
        rel_dir = str(Path(rel_path).parent).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        folder_id = await ensure_folder_path(entity_id, rel_dir)

        async with async_session() as db:
            existing = await db.execute(
                select(Document.id).where(
                    Document.entity_id == entity_id,
                    Document.fs_path == rel_path,
                ).limit(1)
            )
            was_new = existing.scalar_one_or_none() is None
            doc = await upsert_document_by_fs_path(
                db,
                entity_id,
                name=path.name,
                fs_path=rel_path,
                file_size=path.stat().st_size,
                file_type=file_type,
                mime_type=mime_type,
                source=source,
                created_by=user_id or None,
                folder_id=folder_id,
            )
            doc.source = source
            doc.vector_status = "skipped"
            if user_id:
                doc.created_by = user_id
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                origin=origin,
                artifact={"role": "final", "storage_scope": "artifact"},
            )
            await db.commit()
            logger.info("Registered generated file: %s (doc %s)", path.name, doc.id)
            return was_new
    except Exception:
        logger.exception("Failed to register local file %s", path)
        return False


async def _register_url(
    url: str,
    *,
    entity_id: str,
    user_id: str,
    source: str,
    display_name: str | None = None,
    origin: dict | None = None,
) -> bool:
    """Register a remote URL as a document."""
    try:
        from packages.core.database import async_session
        from packages.core.models.document import Document
        from packages.core.services.document_metadata import merge_document_metadata
        from packages.core.services.document_service import create_document
        from sqlalchemy import select

        # Derive name from URL
        from urllib.parse import urlparse, unquote
        parsed = urlparse(url)
        name = display_name or unquote(parsed.path.split("/")[-1]) or "generated_file"
        # Trim query params from name
        if "?" in name:
            name = name.split("?")[0]

        ext = Path(name).suffix.lower()
        file_type, mime_type = _EXT_TO_TYPE.get(ext, ("", "application/octet-stream"))
        origin = dict(origin or {})

        async with async_session() as db:
            # Check if already registered (by file_url)
            existing = await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.file_url == url,
                )
            )
            doc = existing.scalar_one_or_none()
            if doc:
                doc.metadata_ = merge_document_metadata(
                    doc.metadata_,
                    origin=origin,
                    artifact={"role": "final", "storage_scope": "artifact"},
                    external={"source_url": url},
                )
                if display_name and _looks_generated_name(doc.name or ""):
                    doc.name = display_name
                    await db.commit()
                    logger.info("Renamed generated URL document: %s (doc %s)", display_name, doc.id)
                    return True
                await db.commit()
                return False  # Already registered

            doc = await create_document(
                db,
                entity_id,
                name=name,
                file_url=url,
                file_type=file_type,
                mime_type=mime_type,
                source=source,
                created_by=user_id or None,
                metadata=merge_document_metadata(
                    origin=origin,
                    artifact={"role": "final", "storage_scope": "artifact"},
                    external={"source_url": url},
                ),
            )
            doc.vector_status = "skipped"
            await db.commit()
            logger.info("Registered generated URL: %s (doc %s)", name, doc.id)
            return True
    except Exception:
        logger.exception("Failed to register URL %s", url)
        return False
