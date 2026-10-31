"""Bounded storage for room-owned attachments; callers retain authorization."""
from __future__ import annotations

import re
from hashlib import sha256
from typing import BinaryIO

import anyio
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.chat_room import ChatRoomFile, ChatRoomFileChunk


ROOM_FILE_MAX_BYTES = 1024 * 1024 * 1024
ROOM_FILES_TOTAL_MAX_BYTES = 10 * ROOM_FILE_MAX_BYTES
ROOM_FILE_INLINE_MAX_BYTES = 100 * 1024 * 1024
ROOM_FILE_CHUNK_BYTES = 4 * 1024 * 1024


class RoomFileStorageError(ValueError):
    """The persisted attachment metadata and bytes do not agree."""


def _chunk_metadata(row: ChatRoomFile) -> tuple[int, str]:
    if (
        row.content is not None or row.document_id is not None
        or row.document_entity_id is not None or row.source_run_id is not None
        or row.source_media_job_id is not None
        or not 0 < row.size <= ROOM_FILE_MAX_BYTES
        or row.chunk_count != (row.size + ROOM_FILE_CHUNK_BYTES - 1) // ROOM_FILE_CHUNK_BYTES
        or not row.content_sha256
        or re.fullmatch(r"[0-9a-f]{64}", row.content_sha256.lower()) is None
    ):
        raise RoomFileStorageError("Invalid chunked room file metadata")
    return row.chunk_count, row.content_sha256.lower()


async def store_room_file_chunks(
    db: AsyncSession, attachment: ChatRoomFile, source: BinaryIO,
) -> None:
    """Insert a flushed parent's chunks, validating the staged size and hash.

    The caller sets chunk_count and content_sha256 before flushing the parent.
    All inserts belong to that transaction; a failure must roll it back.
    """
    count, expected_digest = _chunk_metadata(attachment)
    await anyio.to_thread.run_sync(source.seek, 0)
    digest = sha256()
    total = 0
    for index in range(count):
        expected_size = min(ROOM_FILE_CHUNK_BYTES, attachment.size - total)
        chunk = await anyio.to_thread.run_sync(source.read, expected_size)
        if len(chunk) != expected_size:
            raise RoomFileStorageError("Incomplete room file content")
        total += len(chunk)
        digest.update(chunk)
        # Core inserts do not retain every chunk in the ORM identity map.
        await db.execute(insert(ChatRoomFileChunk.__table__).values(
            file_id=attachment.id, chunk_index=index, content=chunk,
        ))
    if await anyio.to_thread.run_sync(source.read, 1) or digest.hexdigest() != expected_digest:
        raise RoomFileStorageError("Room file content changed")


async def copy_room_file_content(
    db: AsyncSession, row: ChatRoomFile, target: BinaryIO,
) -> None:
    """Copy an already-authorized room copy to a private target in bounded chunks.

    The target may contain incomplete bytes on failure and must be discarded.
    Generated Knowledge attachments keep their separate provenance reader.
    """
    if row.chunk_count is None:
        if (
            row.document_id is not None or row.document_entity_id is not None
            or row.source_run_id is not None or row.source_media_job_id is not None
            or row.content_sha256 is not None or row.content is None
            or not 0 < row.size <= ROOM_FILE_INLINE_MAX_BYTES
            or len(row.content) != row.size
        ):
            raise RoomFileStorageError("Invalid inline room file content")
        for offset in range(0, row.size, ROOM_FILE_CHUNK_BYTES):
            await anyio.to_thread.run_sync(target.write, row.content[offset:offset + ROOM_FILE_CHUNK_BYTES])
        return

    count, expected_digest = _chunk_metadata(row)
    actual_count = (await db.execute(select(func.count()).select_from(ChatRoomFileChunk).where(
        ChatRoomFileChunk.file_id == row.id,
    ))).scalar_one()
    if actual_count != count:
        raise RoomFileStorageError("Incomplete room file chunks")
    total = 0
    digest = sha256()
    for index in range(count):
        # Fetch one scalar blob at a time, never materialize a file-sized result.
        chunk = (await db.execute(select(ChatRoomFileChunk.content).where(
            ChatRoomFileChunk.file_id == row.id,
            ChatRoomFileChunk.chunk_index == index,
        ))).scalar_one_or_none()
        expected_size = min(ROOM_FILE_CHUNK_BYTES, row.size - total)
        if chunk is None or len(chunk) != expected_size:
            raise RoomFileStorageError("Invalid room file chunk sequence")
        total += len(chunk)
        digest.update(chunk)
        await anyio.to_thread.run_sync(target.write, chunk)
    if total != row.size or digest.hexdigest() != expected_digest:
        raise RoomFileStorageError("Room file content changed")
