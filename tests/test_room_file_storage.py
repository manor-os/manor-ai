"""Room storage uses bounded SQL blobs and rejects incomplete/corrupt copies."""
from __future__ import annotations

import io
from hashlib import sha256

import pytest
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from packages.core.models.chat_room import ChatRoomFile, ChatRoomFileChunk
from packages.core.services.room_file_storage import (
    ROOM_FILE_CHUNK_BYTES, ROOM_FILE_MAX_BYTES, RoomFileStorageError,
    copy_room_file_content, store_room_file_chunks,
)


pytestmark = pytest.mark.unit


class _Database:
    """Run the production SQL statements against an isolated SQLite database."""

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement):
        return self.connection.execute(statement)


@pytest.fixture
def storage_db():
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        for name in ("chat_room_messages", "documents", "chat_room_agent_runs"):
            connection.execute(text(f"CREATE TABLE {name} (id VARCHAR(26) PRIMARY KEY)"))
        ChatRoomFile.__table__.create(connection)
        ChatRoomFileChunk.__table__.create(connection)
        connection.execute(text("INSERT INTO chat_room_messages(id) VALUES ('message')"))
        connection.commit()
        yield _Database(connection)
    engine.dispose()


def _attachment(db, data: bytes) -> ChatRoomFile:
    values = dict(
        id="file", message_id="message", name="example.txt", mime_type="text/plain",
        size=len(data), content=None,
        chunk_count=(len(data) + ROOM_FILE_CHUNK_BYTES - 1) // ROOM_FILE_CHUNK_BYTES,
        content_sha256=sha256(data).hexdigest(),
    )
    db.connection.execute(insert(ChatRoomFile.__table__).values(**values))
    return ChatRoomFile(**values)


class _BoundedSource(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= ROOM_FILE_CHUNK_BYTES
        return super().read(size)


class _BoundedTarget(io.BytesIO):
    def write(self, data):
        assert len(data) <= ROOM_FILE_CHUNK_BYTES
        return super().write(data)


@pytest.mark.parametrize("size", [1, ROOM_FILE_CHUNK_BYTES, ROOM_FILE_CHUNK_BYTES + 7])
async def test_chunk_storage_roundtrip_and_parent_cascade(storage_db, size):
    data = b"z" * size
    row = _attachment(storage_db, data)
    source = _BoundedSource(data)
    source.seek(size)
    await store_room_file_chunks(storage_db, row, source)
    target = _BoundedTarget()
    await copy_room_file_content(storage_db, row, target)
    assert target.getvalue() == data
    storage_db.connection.execute(delete(ChatRoomFile).where(ChatRoomFile.id == row.id))
    assert storage_db.connection.execute(select(ChatRoomFileChunk.file_id)).all() == []


@pytest.mark.parametrize("mutation", ["missing", "gap", "extra", "corrupt", "short"])
async def test_chunk_copy_rejects_invalid_content(storage_db, mutation):
    data = b"z" * (ROOM_FILE_CHUNK_BYTES + 7)
    row = _attachment(storage_db, data)
    await store_room_file_chunks(storage_db, row, io.BytesIO(data))
    table = ChatRoomFileChunk.__table__
    if mutation == "missing":
        storage_db.connection.execute(delete(table).where(table.c.chunk_index == 1))
    elif mutation == "gap":
        storage_db.connection.execute(update(table).where(table.c.chunk_index == 1).values(chunk_index=2))
    elif mutation == "extra":
        storage_db.connection.execute(insert(table).values(file_id=row.id, chunk_index=2, content=b"extra"))
    else:
        storage_db.connection.execute(update(table).where(table.c.chunk_index == 1).values(
            content=b"corrupt" if mutation == "corrupt" else b"short",
        ))
    with pytest.raises(RoomFileStorageError):
        await copy_room_file_content(storage_db, row, _BoundedTarget())


@pytest.mark.parametrize("actual", [b"shor", b"wrong", b"longer"])
async def test_chunk_store_rejects_changed_staging_bytes(storage_db, actual):
    row = _attachment(storage_db, b"exact")
    with pytest.raises(RoomFileStorageError):
        await store_room_file_chunks(storage_db, row, _BoundedSource(actual))
    # The parent and its partial chunks share the caller's transaction.
    storage_db.connection.rollback()
    assert storage_db.connection.execute(select(ChatRoomFile.id)).all() == []
    assert storage_db.connection.execute(select(ChatRoomFileChunk.file_id)).all() == []


async def test_legacy_inline_copy_remains_supported(storage_db):
    row = ChatRoomFile(id="legacy", message_id="message", name="legacy.txt", mime_type="text/plain",
                       size=5, content=b"exact")
    target = _BoundedTarget()
    await copy_room_file_content(storage_db, row, target)
    assert target.getvalue() == b"exact"
    row.size = 6
    with pytest.raises(RoomFileStorageError):
        await copy_room_file_content(storage_db, row, _BoundedTarget())


@pytest.mark.parametrize("size, count, accepted", [
    (ROOM_FILE_MAX_BYTES, 256, True),
    (ROOM_FILE_MAX_BYTES + 1, 257, False),
    (1, None, False),
    (1, 0, False),
    (1, 2, False),
])
def test_storage_constraint_bounds_large_metadata(storage_db, size, count, accepted):
    statement = insert(ChatRoomFile.__table__).values(
        id="file", message_id="message", name="large.dat", mime_type="application/octet-stream",
        size=size, content=None, chunk_count=count, content_sha256="a" * 64,
    )
    if accepted:
        storage_db.connection.execute(statement)
    else:
        with pytest.raises(IntegrityError):
            storage_db.connection.execute(statement)
