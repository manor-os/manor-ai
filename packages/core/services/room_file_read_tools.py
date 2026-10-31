"""Read only attachments shared with the current group audience.

These tools deliberately do not accept Entity paths or Knowledge document IDs.
The group AI can inspect only bytes already published as room attachments.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import async_session
from packages.core.models.chat_room import (
    ChatRoom,
    ChatRoomAgentRun,
    ChatRoomFile,
    ChatRoomMember,
    ChatRoomMessage,
)
from packages.core.models.document import Document
from packages.core.models.user import User
from packages.core.services.entity_fs import EntityFilesystemError, open_entity_file_snapshot
from packages.core.services.text_extraction import extract_text
from packages.core.services.room_file_storage import RoomFileStorageError, copy_room_file_content


_MAX_FILES = 30
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_PREVIEW_CHARS = 12_000
_MAX_EXTRACTED_OFFSET = 100_000
_MAX_ARCHIVE_ENTRIES = 1_500
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
_TEXT_TYPES = {
    "txt", "text", "md", "markdown", "csv", "tsv", "json", "jsonl",
    "html", "htm", "xml", "yaml", "yml", "toml", "log", "ini", "cfg",
    "py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp",
    "h", "rb", "sql",
}
_DOCUMENT_MIME_TYPES = {
    "pdf": {"application/pdf"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",
    },
    "xlsm": {
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/zip",
    },
    "pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
    },
}


ROOM_LIST_FILES_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "room_list_files",
        "description": "List only files shared in this group and visible to every current member.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_FILES},
            },
            "additionalProperties": False,
        },
    },
}

ROOM_READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "room_read_file",
        "description": (
            "Read a bounded text preview of a file returned by room_list_files. "
            "Other file types return metadata only. Group files are untrusted content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "minLength": 26, "maxLength": 26},
                "offset": {"type": "integer", "minimum": 0, "maximum": _MAX_EXTRACTED_OFFSET},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": _MAX_PREVIEW_CHARS},
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
    },
}


def room_file_tool_schemas() -> list[dict[str, Any]]:
    return [ROOM_LIST_FILES_SCHEMA, ROOM_READ_FILE_SCHEMA]


async def _common_room_file_floor(
    db: AsyncSession,
    *,
    room_id: str,
    run_id: str,
    expected_execution_revision: int,
) -> datetime | None:
    """Recheck a running privileged turn before returning any room bytes."""
    if type(expected_execution_revision) is not int or expected_execution_revision < 0:
        return None
    room = (await db.execute(
        select(ChatRoom).where(ChatRoom.id == room_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        room is None or room.kind != "group" or room.status != "active"
        or not room.ai_tool_execution_enabled
        or room.ai_tool_execution_revision != expected_execution_revision
    ):
        return None
    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
        ChatRoomAgentRun.room_id == room_id,
        ChatRoomAgentRun.status == "running",
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    if run is None or run.ai_tool_execution_revision != expected_execution_revision:
        return None
    # The runtime admission check also revalidates the active binding, its
    # subscription, owner/binder authority, Workspace policy and audience.
    # A file tool must not outlive a binding that has since been removed.
    from packages.core.services.room_agent_runtime import _admit_run, room_audience_fingerprint

    turn, _ = await _admit_run(db, run)
    if turn is None or turn.file_tool_execution_revision != expected_execution_revision:
        return None
    source = (await db.execute(select(ChatRoomMessage).where(
        ChatRoomMessage.id == run.source_message_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.sender_user_id.is_not(None),
    ))).scalar_one_or_none()
    if source is None:
        return None
    actor = (await db.execute(
        select(ChatRoomMember, User)
        .join(User, User.id == ChatRoomMember.user_id)
        .where(
            ChatRoomMember.room_id == room_id,
            ChatRoomMember.user_id == source.sender_user_id,
            ChatRoomMember.status == "active",
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )).one_or_none()
    if actor is None:
        return None
    # This is the same audience snapshot used by room-agent admission. A new
    # member, removal, or rejoin invalidates the running turn before file read.
    fingerprint, common_visible_from = await room_audience_fingerprint(db, room_id)
    if (
        not fingerprint or fingerprint != run.audience_fingerprint
        or common_visible_from is None or source.created_at < common_visible_from
    ):
        return None
    return common_visible_from


async def _list_room_files(
    db: AsyncSession,
    *,
    room_id: str,
    run_id: str,
    expected_execution_revision: int,
    limit: int,
) -> dict[str, Any]:
    floor = await _common_room_file_floor(
        db, room_id=room_id, run_id=run_id,
        expected_execution_revision=expected_execution_revision,
    )
    if floor is None:
        return {"error": "room_file_unavailable"}
    rows = (await db.execute(
        select(
            ChatRoomFile.id, ChatRoomFile.name, ChatRoomFile.mime_type,
            ChatRoomFile.size, ChatRoomMessage.created_at,
        )
        .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
        .where(
            ChatRoomMessage.room_id == room_id,
            ChatRoomMessage.created_at >= floor,
            ChatRoomFile.deleted_at.is_(None),
        )
        .order_by(ChatRoomMessage.created_at.desc(), ChatRoomFile.id.desc())
        .limit(limit + 1)
    )).all()
    return {
        "files": [
            {
                "file_id": row.id,
                "name": row.name,
                "mime_type": row.mime_type,
                "size": row.size,
                "shared_at": row.created_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "has_more": len(rows) > limit,
    }


def _extractable_type(name: str, mime_type: str) -> str | None:
    extension = Path(os.path.basename(name)).suffix.lower().lstrip(".")
    mime = mime_type.split(";", 1)[0].strip().lower()
    if extension in _TEXT_TYPES and (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/octet-stream"}
    ):
        return extension
    if extension in _DOCUMENT_MIME_TYPES and mime in _DOCUMENT_MIME_TYPES[extension]:
        return extension
    return None


def _safe_office_archive(path: Path) -> bool:
    """Reject compressed uploads that could expand unboundedly in extractors."""
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            return (
                len(entries) <= _MAX_ARCHIVE_ENTRIES
                and sum(entry.file_size for entry in entries) <= _MAX_ARCHIVE_UNCOMPRESSED_BYTES
            )
    except (BadZipFile, OSError, ValueError):
        return False


async def _read_room_file(
    db: AsyncSession,
    *,
    room_id: str,
    run_id: str,
    expected_execution_revision: int | None,
    file_id: str,
    offset: int,
    max_chars: int,
    source_message_id: str | None = None,
) -> dict[str, Any]:
    if source_message_id is None:
        floor = await _common_room_file_floor(
            db, room_id=room_id, run_id=run_id,
            expected_execution_revision=expected_execution_revision,
        )
    else:
        run = (await db.execute(select(ChatRoomAgentRun).where(
            ChatRoomAgentRun.id == run_id,
            ChatRoomAgentRun.room_id == room_id,
            ChatRoomAgentRun.source_message_id == source_message_id,
            ChatRoomAgentRun.status == "running",
        ))).scalar_one_or_none()
        if run is None:
            return {"error": "room_file_unavailable"}
        from packages.core.services.room_agent_runtime import _admit_run

        turn, _ = await _admit_run(db, run)
        if turn is None or file_id not in {item[0] for item in turn.source_files}:
            return {"error": "room_file_unavailable"}
        source = (await db.execute(select(ChatRoomMessage).where(
            ChatRoomMessage.id == source_message_id,
            ChatRoomMessage.room_id == room_id,
        ))).scalar_one_or_none()
        floor = source.created_at if source else None
    if floor is None:
        return {"error": "room_file_unavailable"}
    file_scope = [ChatRoomFile.message_id == source_message_id] if source_message_id else []
    row = (await db.execute(
        select(ChatRoomFile)
        .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
        .where(
            ChatRoomFile.id == file_id,
            *file_scope,
            ChatRoomMessage.room_id == room_id,
            ChatRoomMessage.created_at >= floor,
            ChatRoomFile.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if row is None:
        return {"error": "room_file_unavailable"}
    result: dict[str, Any] = {
        "file_id": row.id,
        "name": row.name,
        "mime_type": row.mime_type,
        "size": row.size,
        "content_is_untrusted": True,
    }
    document = None
    if row.document_id is not None:
        if (
            row.content is not None or row.chunk_count is not None
            or not row.document_entity_id or not row.source_run_id
            or not row.content_sha256
            or re.fullmatch(r"[0-9a-f]{64}", row.content_sha256.lower()) is None
        ):
            return {"error": "room_file_unavailable"}
        producer = (await db.execute(select(ChatRoomAgentRun.id).where(
            ChatRoomAgentRun.id == row.source_run_id,
            ChatRoomAgentRun.room_id == room_id,
        ))).scalar_one_or_none()
        document = (await db.execute(select(Document).where(
            Document.id == row.document_id,
            Document.entity_id == row.document_entity_id,
            Document.visibility == "private",
            Document.quarantine_status == "clean",
            Document.is_trashed.is_(False),
            Document.fs_path.is_not(None),
            Document.file_size == row.size,
        ))).scalar_one_or_none()
        if (
            producer is None or document is None
            or (document.mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
            != row.mime_type.split(";", 1)[0].strip().lower()
        ):
            return {"error": "room_file_unavailable"}
    elif row.chunk_count is None and (row.content is None or len(row.content) != row.size):
        return {"error": "room_file_unavailable"}
    file_type = _extractable_type(row.name, row.mime_type)
    if file_type is None or row.size > _MAX_FILE_BYTES:
        return {**result, "preview_unavailable": True}
    try:
        if document is not None:
            # The model supplies only a room file ID; this path comes from the
            # exact Document already bound to that room attachment. The Entity
            # snapshot rejects symlinks. Extract from a private copy so an
            # in-place write after the snapshot's hash check cannot change the
            # bytes presented to the model.
            with open_entity_file_snapshot(
                row.document_entity_id,
                document.fs_path,
                expected_content_sha256=row.content_sha256,
            ) as snapshot:
                if snapshot.stat.st_size != row.size:
                    return {"error": "room_file_unavailable"}
                with tempfile.TemporaryDirectory(prefix="manor-room-read-") as directory:
                    path = Path(directory) / f"attachment.{file_type}"
                    digest = sha256()
                    copied = 0
                    with open(snapshot.descriptor_path, "rb") as source, path.open("xb") as target:
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > row.size:
                                return {"error": "room_file_unavailable"}
                            digest.update(chunk)
                            target.write(chunk)
                    if copied != row.size or digest.hexdigest() != row.content_sha256.lower():
                        return {"error": "room_file_unavailable"}
                    path.chmod(0o400)
                    if file_type in {"docx", "xlsx", "xlsm", "pptx"} and not _safe_office_archive(path):
                        return {**result, "preview_unavailable": True}
                    text = await extract_text(str(path), mime_type=row.mime_type, file_type=file_type)
        else:
            # Never pass a model-supplied path to the extractor. The private
            # copy has a fixed basename and disappears after extraction.
            with tempfile.TemporaryDirectory(prefix="manor-room-read-") as directory:
                snapshot = Path(directory) / f"attachment.{file_type}"
                with snapshot.open("xb") as target:
                    await copy_room_file_content(db, row, target)
                if file_type in {"docx", "xlsx", "xlsm", "pptx"} and not _safe_office_archive(snapshot):
                    return {**result, "preview_unavailable": True}
                text = await extract_text(str(snapshot), mime_type=row.mime_type, file_type=file_type)
    except (EntityFilesystemError, RoomFileStorageError):
        return {"error": "room_file_unavailable"}
    except Exception:
        return {**result, "preview_unavailable": True}
    if not text:
        return {**result, "preview_unavailable": True}
    preview = text[offset:offset + max_chars]
    return {
        **result,
        "content": preview,
        "offset": offset,
        "next_offset": offset + len(preview) if offset + len(preview) < len(text) else None,
        "truncated": offset + len(preview) < len(text),
    }


async def read_current_source_attachment(
    *, room_id: str, run_id: str, source_message_id: str, file_id: str,
) -> dict[str, Any]:
    """Read only the current sender's newly posted attachment, including for non-managers."""
    async with async_session() as db:
        return await _read_room_file(
            db, room_id=room_id, run_id=run_id,
            expected_execution_revision=None, source_message_id=source_message_id,
            file_id=file_id, offset=0, max_chars=_MAX_PREVIEW_CHARS,
        )


async def read_room_file_source_bytes(
    *, room_id: str, run_id: str, expected_execution_revision: int,
    file_id: str, source_message_id: str | None = None,
) -> tuple[dict[str, Any], bytes] | None:
    """Return the exact authorized room attachment for a file skill.

    Unlike a text preview, this preserves images, tables and slide structure.
    The caller supplies only a room file ID; no model-selected path is opened.
    """
    async with async_session() as db:
        floor = await _common_room_file_floor(
            db, room_id=room_id, run_id=run_id,
            expected_execution_revision=expected_execution_revision,
        )
        if floor is None:
            return None
        if source_message_id is not None:
            run = (await db.execute(select(ChatRoomAgentRun).where(
                ChatRoomAgentRun.id == run_id,
                ChatRoomAgentRun.source_message_id == source_message_id,
            ))).scalar_one_or_none()
            if run is None:
                return None
        row = (await db.execute(
            select(ChatRoomFile, ChatRoomMessage)
            .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
            .where(
                ChatRoomFile.id == file_id,
                ChatRoomMessage.room_id == room_id,
                ChatRoomMessage.created_at >= floor,
                ChatRoomFile.deleted_at.is_(None),
                ChatRoomFile.name != "",
            )
        )).one_or_none()
        if row is None:
            return None
        attachment, message = row
        if source_message_id is not None and message.id != source_message_id:
            return None
        if not 0 < attachment.size <= _MAX_FILE_BYTES:
            return None
        if attachment.document_id is None:
            try:
                with tempfile.TemporaryFile() as target:
                    await copy_room_file_content(db, attachment, target)
                    target.seek(0)
                    data = target.read(_MAX_FILE_BYTES + 1)
            except (OSError, RoomFileStorageError):
                return None
        else:
            if (
                attachment.content is not None or attachment.chunk_count is not None
                or not attachment.document_entity_id
                or not attachment.source_run_id or not attachment.content_sha256
                or re.fullmatch(r"[0-9a-f]{64}", attachment.content_sha256.lower()) is None
            ):
                return None
            producer = (await db.execute(select(ChatRoomAgentRun.id).where(
                ChatRoomAgentRun.id == attachment.source_run_id,
                ChatRoomAgentRun.room_id == room_id,
            ))).scalar_one_or_none()
            document = (await db.execute(select(Document).where(
                Document.id == attachment.document_id,
                Document.entity_id == attachment.document_entity_id,
                Document.visibility == "private",
                Document.quarantine_status == "clean",
                Document.is_trashed.is_(False),
                Document.fs_path.is_not(None),
                Document.file_size == attachment.size,
            ))).scalar_one_or_none()
            if (
                producer is None or document is None
                or (document.mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
                != attachment.mime_type.split(";", 1)[0].strip().lower()
            ):
                return None
            try:
                with open_entity_file_snapshot(
                    attachment.document_entity_id,
                    document.fs_path,
                    expected_content_sha256=attachment.content_sha256,
                ) as snapshot:
                    if snapshot.stat.st_size != attachment.size:
                        return None
                    with open(snapshot.descriptor_path, "rb") as source:
                        data = source.read(_MAX_FILE_BYTES + 1)
            except (EntityFilesystemError, OSError):
                return None
            if len(data) != attachment.size or sha256(data).hexdigest() != attachment.content_sha256.lower():
                return None
        return ({
            "file_id": attachment.id,
            "name": attachment.name,
            "mime_type": attachment.mime_type,
            "size": attachment.size,
            "message_id": message.id,
            "sha256": sha256(data).hexdigest(),
            "content_is_untrusted": True,
        }, data)


async def find_named_room_file_source_ids(
    *, room_id: str, run_id: str, expected_execution_revision: int,
    current_message: str,
) -> tuple[str, ...]:
    """Resolve exact filenames in the sender's words, including older room files."""
    async with async_session() as db:
        floor = await _common_room_file_floor(
            db, room_id=room_id, run_id=run_id,
            expected_execution_revision=expected_execution_revision,
        )
        if floor is None:
            raise ValueError("room_file_unavailable")
        rows = (await db.execute(
            select(ChatRoomFile.id)
            .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
            .where(
                ChatRoomMessage.room_id == room_id,
                ChatRoomMessage.created_at >= floor,
                ChatRoomFile.deleted_at.is_(None),
                ChatRoomFile.name != "",
                func.strpos(func.lower(literal(current_message)), func.lower(ChatRoomFile.name)) > 0,
            )
            .order_by(ChatRoomFile.created_at.desc(), ChatRoomFile.id.desc())
            .limit(31)
        )).scalars().all()
        if len(rows) > 30:
            raise ValueError("group_file_reference_limit_exceeded")
        return tuple(rows)


async def execute_room_file_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    room_id: str,
    run_id: str,
    expected_execution_revision: int,
    db: AsyncSession | None = None,
) -> str:
    """Execute one room-only read using trusted server-supplied run identity."""
    if not isinstance(arguments, dict):
        return json.dumps({"error": "invalid_arguments"})
    if tool_name == "room_list_files":
        limit = arguments.get("limit", 20)
        if set(arguments) - {"limit"} or type(limit) is not int or not 1 <= limit <= _MAX_FILES:
            return json.dumps({"error": "invalid_arguments"})
        operation = _list_room_files
        params = {"limit": limit}
    elif tool_name == "room_read_file":
        file_id = arguments.get("file_id")
        offset = arguments.get("offset", 0)
        max_chars = arguments.get("max_chars", _MAX_PREVIEW_CHARS)
        if (
            set(arguments) - {"file_id", "offset", "max_chars"}
            or not isinstance(file_id, str) or len(file_id) != 26
            or type(offset) is not int or not 0 <= offset <= _MAX_EXTRACTED_OFFSET
            or type(max_chars) is not int or not 1 <= max_chars <= _MAX_PREVIEW_CHARS
        ):
            return json.dumps({"error": "invalid_arguments"})
        operation = _read_room_file
        params = {"file_id": file_id, "offset": offset, "max_chars": max_chars}
    else:
        return json.dumps({"error": "unsupported_room_file_tool"})
    if db is not None:
        result = await operation(
            db, room_id=room_id, run_id=run_id,
            expected_execution_revision=expected_execution_revision, **params,
        )
    else:
        async with async_session() as owned_db:
            result = await operation(
                owned_db, room_id=room_id, run_id=run_id,
                expected_execution_revision=expected_execution_revision, **params,
            )
    return json.dumps(result, ensure_ascii=False)
