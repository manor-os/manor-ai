"""Read one workspace (entity filesystem) file as tool input, through the ACL gate.

Shared by every tool that copies an existing workspace file somewhere else
(into a sandbox, for instance). The read holds the entity read lock across
authorization and readback, checks Knowledge read ACLs before anything is
opened, and reads from a pinned regular-file snapshot.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from packages.core.ai.runtime.file_actions import (
    runtime_entity_file_root,
    runtime_entity_filesystem_read_lock,
    runtime_guard_file_read_access,
    runtime_open_entity_file_snapshot,
)
from packages.core.services.file_refs import is_internal_file_target, parse_file_reference

logger = logging.getLogger(__name__)

WorkspaceReadStatus = Literal["ok", "not_found", "denied", "too_large", "error"]


@dataclass(frozen=True)
class WorkspaceFileRead:
    """Outcome of ``runtime_read_workspace_file``.

    ``status``:
      - ``"ok"``: ``content`` holds the bytes.
      - ``"not_found"``: the path names no workspace file (also an empty or
        ``..``-bearing path).
      - ``"denied"``: the ACL gate refused; ``blocked`` is its JSON payload.
      - ``"too_large"``: the file exceeds ``max_bytes``; ``size`` is its size.
        Refused before reading, or discarded if it grew past the cap mid-read.
      - ``"error"``: the file could not be read (storage disabled, unsafe
        file, I/O failure).
    """

    status: WorkspaceReadStatus
    content: bytes | None = None
    blocked: str | None = None
    size: int | None = None


def runtime_workspace_path_candidates(workspace_path: str, entity_id: str = "") -> list[str]:
    """Entity-relative path named by any file address (fs URL, /workspace/…, path).

    Any path that escapes the entity root (``..``) yields nothing.
    """

    raw = workspace_path.replace("\\", "/").strip()
    if not raw or raw == "/workspace":
        return []
    parsed = parse_file_reference(raw, entity_id=entity_id, allow_relative=True)
    if parsed is None and raw.startswith("/") and not is_internal_file_target(raw):
        parsed = parse_file_reference(raw.lstrip("/"), entity_id=entity_id, allow_relative=True)
    if parsed is None or not parsed.fs_path or (entity_id and parsed.entity_id not in {"", entity_id}):
        return []
    if ".." in parsed.fs_path:
        return []
    return [parsed.fs_path]


async def runtime_workspace_document_path(entity_id: str, workspace_path: str) -> str:
    """The fs_path of a /viewer/<document_id> address, if it names one."""

    parsed = parse_file_reference(workspace_path.strip(), entity_id=entity_id)
    if parsed is None or not parsed.document_id:
        return ""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import Document

    async with async_session() as db:
        fs_path = await db.scalar(select(Document.fs_path).where(
            Document.id == parsed.document_id,
            Document.entity_id == entity_id,
            Document.is_trashed == False,  # noqa: E712
        ))
    return str(fs_path or "")


async def runtime_read_workspace_file(
    *,
    entity_id: str,
    workspace_path: str,
    tool_name: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    runtime_envelope: Any | None = None,
    max_bytes: int | None = None,
) -> WorkspaceFileRead:
    """Read ``workspace_path`` for ``tool_name``; see ``WorkspaceFileRead``.

    With ``max_bytes`` the file is stat'ed first and an oversize file is
    refused without reading it. Raises only when the entity root itself is
    unsafe (``runtime_entity_file_root``).
    """

    candidates = runtime_workspace_path_candidates(workspace_path, entity_id)
    if not candidates:
        document_path = await runtime_workspace_document_path(entity_id, workspace_path)
        candidates = [document_path] if document_path and ".." not in document_path else []
    if not candidates:
        return WorkspaceFileRead("not_found")

    rel_path = candidates[0]
    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return WorkspaceFileRead("error")
    try:
        async with runtime_entity_filesystem_read_lock(entity_root):
            blocked = await runtime_guard_file_read_access(
                entity_id=entity_id,
                user_id=user_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                runtime_envelope=runtime_envelope,
                tool_name=tool_name,
                paths=[rel_path],
            )
            if blocked:
                return WorkspaceFileRead("denied", blocked=blocked)
            with runtime_open_entity_file_snapshot(entity_id, rel_path) as snapshot:
                size = int(snapshot.stat.st_size)
                if max_bytes is not None and size > max_bytes:
                    return WorkspaceFileRead("too_large", size=size)
                with open(snapshot.descriptor_path, "rb") as handle:
                    if max_bytes is None:
                        return WorkspaceFileRead("ok", content=handle.read())
                    content = handle.read(max_bytes + 1)
                    if len(content) > max_bytes:
                        # Grew past the cap after the size check.
                        return WorkspaceFileRead("too_large", size=os.fstat(handle.fileno()).st_size)
                return WorkspaceFileRead("ok", content=content)
    except Exception as exc:  # noqa: BLE001 - classified below, never raised
        logger.debug("[workspace_read] read failed path=%s: %s", workspace_path, exc)
        return WorkspaceFileRead("not_found" if _is_missing_path(exc) else "error")


# A path component that does not exist, or one that is not a directory: either
# way the path names no file.
_MISSING_PATH_ERRORS = (FileNotFoundError, NotADirectoryError)


def _is_missing_path(exc: BaseException) -> bool:
    """Whether ``exc`` (or an error it wraps) says the path names nothing.

    Decided from the exception alone: no extra filesystem probe, which could
    follow a symlinked component outside the entity root.
    """

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, _MISSING_PATH_ERRORS):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False
