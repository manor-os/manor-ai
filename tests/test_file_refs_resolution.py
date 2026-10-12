"""A file address resolves only to a file this user can open; uploads open for their uploader."""
from __future__ import annotations

import hashlib

import pytest
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.services.file_refs import (
    ParsedFileRef,
    chat_upload_access,
    parse_file_reference,
    resolve_openable_file,
)
from tests.test_chat_attachment_read_access import attachment_fs, seed_attachment  # noqa: F401
from tests.test_document_permissions import _auth, _invite_and_accept_member


@pytest.mark.asyncio
async def test_unprojected_upload_opens_for_its_uploader_only(db_session, attachment_fs):
    user, _conv, _msg, _ws, _source, path = await seed_attachment(db_session, attachment_fs)
    access = await chat_upload_access(db_session, entity_id=user.entity_id, user_id=user.id, fs_path=path)
    assert access == "uploader"
    assert await chat_upload_access(
        db_session, entity_id=user.entity_id, user_id=generate_ulid(), fs_path=path,
    ) is None

    url = f"/api/v1/fs/{user.entity_id}/{path}"
    ref = await resolve_openable_file(
        db_session, parse_file_reference(url), entity_id=user.entity_id, user_id=user.id,
    )
    assert ref is not None and ref["open_url"] == url
    assert await resolve_openable_file(
        db_session, parse_file_reference(url), entity_id=user.entity_id, user_id=generate_ulid(),
    ) is None


@pytest.mark.asyncio
async def test_resolution_rejects_missing_foreign_and_hidden_files(db_session, attachment_fs):
    user, _conv, _msg, _ws, _source, _path = await seed_attachment(db_session, attachment_fs)
    entity_root = attachment_fs / user.entity_id
    (entity_root / "tasks").mkdir()
    (entity_root / "tasks" / "scratch.png").write_bytes(b"x")
    (entity_root / "Decks").mkdir()
    (entity_root / "Decks" / "deck one.pptx").write_bytes(b"pptx")

    async def resolve(parsed):
        return await resolve_openable_file(db_session, parsed, entity_id=user.entity_id, user_id=user.id)

    assert await resolve(ParsedFileRef(user.entity_id, "Decks/missing.pptx")) is None
    assert await resolve(ParsedFileRef(generate_ulid(), "Decks/deck one.pptx")) is None
    assert await resolve(ParsedFileRef(user.entity_id, "tasks/scratch.png")) is None
    assert await resolve(ParsedFileRef(user.entity_id, "", "01NOSUCHDOCUMENT")) is None
    # An attachment someone else never sent in this user's conversation.
    stray = "uploads/chat/0123456789abcdef.png"
    (entity_root / stray).write_bytes(b"png")
    assert await resolve(ParsedFileRef(user.entity_id, stray)) is None


@pytest.mark.asyncio
async def test_raw_upload_url_opens_for_uploader_not_for_other_members(
    client: AsyncClient, db_session, attachment_fs,
):
    owner = await _auth(client, "upload_open_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    member, _ = await _invite_and_accept_member(client, owner, "upload.open.member@test.com")
    data = b"attached by the owner"
    path = f"uploads/chat/{hashlib.sha256(data).hexdigest()[:16]}.txt"
    conversation = Conversation(entity_id=me["entity_id"], user_id=me["id"], scope="channel", status="active")
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(
        conversation_id=conversation.id, role="user", author_kind="user",
        attachments=[{"kind": "chat_upload", "path": path}],
    ))
    await db_session.commit()
    target = attachment_fs / me["entity_id"] / path
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    url = f"/api/v1/fs/{me['entity_id']}/{path}"
    response = await client.get(url, headers=owner)
    assert response.status_code == 200, response.text
    assert response.content == data
    read = await client.get("/api/v1/fs/read", headers=owner, params={"path": path})
    assert read.status_code == 200, read.text

    assert (await client.get(url, headers=member)).status_code in {403, 404}
    assert (await client.get("/api/v1/fs/read", headers=member, params={"path": path})).status_code in {403, 404}

    # A file in the upload area that no message attached stays closed.
    stray = attachment_fs / me["entity_id"] / "uploads/chat/0123456789abcdef.txt"
    stray.write_bytes(b"stray")
    assert (await client.get(
        f"/api/v1/fs/{me['entity_id']}/uploads/chat/0123456789abcdef.txt", headers=owner,
    )).status_code == 403
