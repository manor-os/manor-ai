"""Inspect current-conversation media without widening attachment read access."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


async def inspect_chat_attachment_media(
    reference: Any,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str | None,
    workspace_id: str | None,
    tool_name: str,
    allowed_suffixes: set[str],
    max_bytes: int,
    inspect_snapshot: Callable[[str], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read one exact persisted attachment through the shared conversation ACL."""
    from packages.core.services.ai_file_permissions import guard_ai_file_read_access, is_chat_upload_path
    from packages.core.services.entity_fs import (
        EntityFilesystemError, canonical_entity_root, entity_filesystem_read_lock,
        is_fs_enabled, open_entity_file_snapshot,
    )

    if (
        not isinstance(reference, dict)
        or set(reference) != {"source_kind", "path"}
        or reference.get("source_kind") != "chat_attachment"
        or not isinstance(reference.get("path"), str)
        or not is_chat_upload_path(reference["path"])
    ):
        raise ValueError("Use an exact uploaded chat attachment reference.")
    if not all((entity_id, user_id, conversation_id, tool_name)):
        raise ValueError("Chat media requires the current user and conversation.")
    path = reference["path"]
    if Path(path).suffix.lower() not in allowed_suffixes:
        raise ValueError("The chat attachment has an unsupported media type.")
    if not is_fs_enabled():
        raise ValueError("The chat attachment filesystem is unavailable.")
    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            denied = await guard_ai_file_read_access(
                entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
                workspace_id=workspace_id, tool_name=tool_name, paths=[path],
            )
            if denied:
                raise ValueError("The chat attachment is no longer readable in this conversation.")
            with open_entity_file_snapshot(entity_id, path) as snapshot:
                if not 0 < snapshot.stat.st_size <= max_bytes:
                    raise ValueError("The chat attachment has an invalid size.")

                def digest_file() -> str:
                    with open(snapshot.descriptor_path, "rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                        source.seek(0)
                        return digest

                digest = snapshot.content_sha256 or await asyncio.to_thread(digest_file)
                details = inspect_snapshot(snapshot.descriptor_path) if inspect_snapshot else {}
                if inspect.isawaitable(details):
                    details = await details
                return {
                    "source_kind": "chat_attachment", "rel_path": path,
                    "source_sha256": digest, "size_bytes": snapshot.stat.st_size,
                    **dict(details or {}),
                }
    except (EntityFilesystemError, OSError) as exc:
        raise ValueError("The chat attachment is unavailable or has changed.") from exc
