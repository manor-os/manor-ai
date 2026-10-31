"""Workspace Knowledge projection is distinct from public chat attachment use."""
from datetime import datetime, timezone
import asyncio
import io

import pytest
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.ai.runtime import ChatSurface
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.file_context import _register_workspace_chat_upload


async def _setup(db, role="owner", membership=None, foreign=False):
    entity = Entity(name="Upload test")
    other = Entity(name="Other test")
    db.add_all([entity, other])
    await db.flush()
    user = User(entity_id=other.id if foreign else entity.id, email=f"{entity.id}@test.com",
                password_hash="x", role=role)
    workspace = Workspace(entity_id=entity.id, name="Uploads")
    db.add_all([user, workspace])
    await db.flush()
    if membership:
        db.add(WorkspaceStaff(workspace_id=workspace.id, user_id=user.id, role=membership, status="active"))
        await db.flush()
    return entity, user, workspace


async def _register(db, entity, user, workspace, surface=ChatSurface.WORKSPACE_CHAT):
    return await _register_workspace_chat_upload(
        db, entity_id=entity.id, user_id=user.id if user else None, workspace_id=workspace.id,
        surface=surface, filename="cover.png", saved_rel_path="uploads/chat/cover.png",
        saved_url=f"/api/v1/fs/{entity.id}/uploads/chat/cover.png", file_size=10, mime="image/png",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role,membership,foreign,surface,anonymous", [
    ("owner", None, False, ChatSurface.PUBLIC_CUSTOMER_CHAT, True),
    ("client", None, False, ChatSurface.PUBLIC_CUSTOMER_CHAT, False),
    ("owner", None, False, ChatSurface.EXTERNAL_CHANNEL_CHAT, False),
    ("owner", None, False, None, False),
    ("owner", None, False, ChatSurface.WORKSPACE_CHAT, True),
    ("member", "viewer", False, ChatSurface.WORKSPACE_CHAT, False),
    ("member", None, False, ChatSurface.WORKSPACE_CHAT, False),
    ("owner", None, True, ChatSurface.WORKSPACE_CHAT, False),
])
async def test_unadmitted_upload_does_not_write_knowledge(db_session, role, membership, foreign, surface, anonymous):
    entity, user, workspace = await _setup(db_session, role, membership, foreign)
    assert await _register(db_session, entity, None if anonymous else user, workspace, surface) is None
    for model in (Document, DocumentGroup):
        assert await db_session.scalar(select(func.count()).select_from(model).where(model.entity_id == entity.id)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role,membership", [("owner", None), ("member", "editor")])
async def test_internal_upload_is_owned_and_idempotent(db_session, monkeypatch, role, membership):
    from packages.core.services import document_service
    entity, user, workspace = await _setup(db_session, role, membership)
    calls = []

    async def admission(*args):
        calls.append(True)

    monkeypatch.setattr(document_service, "_enforce_storage_limit", admission)
    doc_id = await _register(db_session, entity, user, workspace)
    assert await _register(db_session, entity, user, workspace) == doc_id
    doc = await db_session.get(Document, doc_id)
    assert doc.owner_id == user.id and doc.visibility == "workspace"
    assert calls == [True]
    assert await db_session.scalar(select(func.count()).select_from(DocumentGroupMember).where(DocumentGroupMember.document_id == doc_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_storage_denial_does_not_create_or_restore_knowledge(db_session, monkeypatch, existing):
    from packages.core.services import document_service
    entity, user, workspace = await _setup(db_session)
    doc = None
    if existing:
        doc = Document(entity_id=entity.id, name="keep", fs_path="uploads/chat/cover.png",
                       source="chat_upload", owner_id=user.id, is_trashed=True,
                       trashed_at=datetime.now(timezone.utc), visibility="private")
        db_session.add(doc)
        await db_session.flush()

    async def deny(*args):
        raise document_service.StorageLimitExceeded("quota", plan="test", limit=1, current=1)

    monkeypatch.setattr(document_service, "_enforce_storage_limit", deny)
    with pytest.raises(document_service.StorageLimitExceeded):
        await _register(db_session, entity, user, workspace)
    assert await db_session.scalar(select(func.count()).select_from(DocumentGroup).where(DocumentGroup.entity_id == entity.id)) == 0
    if doc:
        assert doc.is_trashed and doc.name == "keep" and doc.visibility == "private"


@pytest.mark.asyncio
async def test_same_path_cannot_modify_another_owners_private_document(db_session):
    entity, user, workspace = await _setup(db_session, "member", "editor")
    doc = Document(entity_id=entity.id, name="private", fs_path="uploads/chat/cover.png",
                   owner_id="different-owner", visibility="private", is_trashed=True,
                   trashed_at=datetime.now(timezone.utc), source="upload")
    db_session.add(doc)
    await db_session.flush()
    assert await _register(db_session, entity, user, workspace) is None
    assert doc.name == "private" and doc.is_trashed and doc.visibility == "private" and doc.source == "upload"
    assert await db_session.scalar(select(func.count()).select_from(DocumentGroupMember).where(DocumentGroupMember.document_id == doc.id)) == 0


@pytest.mark.asyncio
async def test_same_content_uploaded_by_two_workspace_users_has_separate_documents(
    db_session, monkeypatch, tmp_path,
):
    from packages.core.config import get_settings
    from packages.core.services import file_context, upload_security

    entity, first_user, workspace = await _setup(db_session, "member", "editor")
    second_user = User(
        entity_id=entity.id, email=f"second-{entity.id}@test.com",
        password_hash="x", role="member",
    )
    db_session.add(second_user)
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=workspace.id, user_id=second_user.id,
        role="editor", status="active",
    ))
    await db_session.flush()

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))

    async def inspect(*_args, **_kwargs):
        return "text/plain"

    async def extract(*_args, **_kwargs):
        return "same content"

    monkeypatch.setattr(upload_security, "inspect_upload_content", inspect)
    monkeypatch.setattr(file_context, "extract_text", extract)

    attachments = []
    for user in (first_user, second_user):
        result = await file_context.build_file_context(
            [UploadFile(filename="same.txt", file=io.BytesIO(b"same content"))],
            [], entity.id, db_session, workspace_id=workspace.id,
            user_id=user.id, surface=ChatSurface.WORKSPACE_CHAT,
        )
        attachments.append(result.attachment_refs[0])

    assert attachments[0]["path"] != attachments[1]["path"]
    assert attachments[0]["document_id"] != attachments[1]["document_id"]
    assert all(attachment["document_id"] for attachment in attachments)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ChatSurface.PUBLIC_CUSTOMER_CHAT, ChatSurface.EXTERNAL_CHANNEL_CHAT])
@pytest.mark.parametrize("mime,name", [("image/png", "cover.png"), ("text/plain", "notes.txt"),
                                      ("video/mp4", "clip.mp4"), ("audio/wav", "song.wav")])
async def test_public_media_stays_available_without_knowledge_registration(monkeypatch, surface, mime, name):
    import io
    from fastapi import UploadFile
    from packages.core.services import file_context, upload_security
    from packages.core.services.voice import whisper

    async def inspect(*args, **kwargs):
        return mime

    async def extract(*args, **kwargs):
        return "attachment contents"

    async def credit(*args, **kwargs):
        return None

    async def transcribe(*args, **kwargs):
        raise whisper.WhisperError("test: no speech")

    url = f"/api/v1/fs/entity/uploads/chat/{name}"
    monkeypatch.setattr(upload_security, "inspect_upload_content", inspect)
    monkeypatch.setattr(file_context, "_save_chat_image", lambda *args: url)
    monkeypatch.setattr(file_context, "_save_chat_upload", lambda *args: (f"uploads/chat/{name}", url))
    monkeypatch.setattr(file_context, "extract_text", extract)
    monkeypatch.setattr(file_context, "runtime_assert_credit_available", credit)
    monkeypatch.setattr(whisper, "transcribe_blob", transcribe)
    # Images must decode to be stored (metadata is stripped first); other types stay opaque.
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (1, 1)).save(png, format="PNG")
    payload = png.getvalue() if mime.startswith("image/") else b"attachment bytes"
    # A DB sentinel proves these surfaces cannot reach Knowledge persistence.
    attachments = await file_context.build_file_context(
        [UploadFile(filename=name, file=io.BytesIO(payload))], [], "entity", object(),
        workspace_id="workspace", user_id="internal-user", surface=surface,
    )
    assert attachments.attachment_refs[0]["url"] == url
    assert not attachments.attachment_refs[0].get("document_id")


@pytest.mark.asyncio
async def test_private_chat_attachment_adapter_marks_internal_surface(monkeypatch):
    from apps.api.routers import chat
    calls = []

    async def prepare(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(chat, "prepare_runtime_file_context_turn", prepare)
    await chat._build_attachments("hello", None, [], "entity", object(), workspace_id="workspace", user_id="user")
    assert calls[0]["surface"] == ChatSurface.WORKSPACE_CHAT


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["delete", "revoke"])
async def test_waiting_upload_rechecks_workspace_and_write_permission(db_session, mutation):
    entity, user, workspace = await _setup(db_session, "member", "editor")
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def register():
        async with sessions() as session:
            # Request auth may already have populated this ORM identity before
            # the upload waits for another transaction's Workspace lock.
            cached_membership = await session.scalar(select(WorkspaceStaff).where(
                WorkspaceStaff.workspace_id == workspace.id, WorkspaceStaff.user_id == user.id,
            ))
            result = await _register(session, entity, user, workspace)
            await session.commit()
            assert cached_membership is not None
            return result

    task = None
    try:
        async with sessions() as changer:
            locked = await changer.scalar(select(Workspace).where(Workspace.id == workspace.id).with_for_update())
            task = asyncio.create_task(register())
            done, _ = await asyncio.wait([task], timeout=0.25)
            assert not done, "Upload must wait for the Workspace authorization boundary"
            if mutation == "delete":
                locked.deleted_at = datetime.now(timezone.utc)
            else:
                membership = await changer.scalar(select(WorkspaceStaff).where(
                    WorkspaceStaff.workspace_id == workspace.id, WorkspaceStaff.user_id == user.id,
                ))
                membership.role = "viewer"
            await changer.commit()
        assert await asyncio.wait_for(task, timeout=5) is None
        assert await db_session.scalar(select(func.count()).select_from(Document).where(Document.entity_id == entity.id)) == 0
        assert await db_session.scalar(select(func.count()).select_from(DocumentGroup).where(DocumentGroup.entity_id == entity.id)) == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_failure_rolls_back_projected_document(db_session, monkeypatch):
    from fastapi import HTTPException
    from packages.core.services import file_context
    entity, user, workspace = await _setup(db_session)

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(file_context, "_ensure_workspace_upload_group", unavailable)
    with pytest.raises(HTTPException):
        await _register(db_session, entity, user, workspace)
    assert await db_session.scalar(select(func.count()).select_from(Document).where(Document.entity_id == entity.id)) == 0
