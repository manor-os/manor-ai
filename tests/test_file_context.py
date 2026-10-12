from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from packages.core.models.document import Document
from packages.core.models.permission import Capability
from packages.core.models.user import Entity
from packages.core.services import file_context


def _upload(name: str, content: bytes, content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        filename=name,
        file=BytesIO(content),
        headers=Headers({"content-type": content_type}),
    )


class _FakeScalars:
    def __init__(self, docs):
        self._docs = docs

    def all(self):
        return self._docs


class _FakeResult:
    def __init__(self, docs):
        self._docs = docs

    def scalars(self):
        return _FakeScalars(self._docs)

    def scalar_one_or_none(self):
        return self._docs[0] if self._docs else None


class _FakeDb:
    def __init__(self, docs, *, entity_available=True):
        self._docs = docs
        self._entity_available = entity_available

    async def execute(self, _stmt):
        return _FakeResult(self._docs)

    async def scalars(self, _stmt):
        return _FakeScalars([])

    async def scalar(self, stmt):
        if any(column.get("entity") is Entity for column in stmt.column_descriptions):
            return "entity_1" if self._entity_available else None
        return None


@pytest.mark.asyncio
async def test_workspace_attachment_cannot_publish_another_users_private_document(monkeypatch):
    from packages.core.services import document_access, document_service

    document = Document(
        id="other-document",
        entity_id="entity_1",
        name="private.md",
        owner_id="other-user",
        visibility="private",
    )

    async def locked_document(db, document_id, entity_id, *, mutation_authorizer, **_kwargs):
        assert document_id == document.id
        assert entity_id == document.entity_id
        await mutation_authorizer(db, document)
        return document

    async def capabilities(*_args, **_kwargs):
        return {Capability.VIEW}

    async def readable(*_args, **_kwargs):
        return True

    monkeypatch.setattr(document_service, "_get_document_for_policy_mutation", locked_document)
    monkeypatch.setattr(document_access, "effective_document_capabilities_for_user", capabilities)
    monkeypatch.setattr(document_access, "user_can_read_document", readable)

    with pytest.raises(HTTPException) as error:
        await file_context.bind_workspace_knowledge_documents(
            _FakeDb([]),
            entity_id="entity_1",
            workspace_id="workspace_1",
            user_id="requester",
            attachments=[{"document_id": document.id}],
        )

    assert error.value.status_code == 404
    assert document.visibility == "private"
    assert document.folder_id is None


@pytest.mark.asyncio
async def test_workspace_attachment_accepts_explicit_internal_share(monkeypatch):
    from packages.core.services import document_access, document_service

    document = Document(
        id="shared-document",
        entity_id="entity_1",
        name="shared.md",
        owner_id="other-user",
        visibility="private",
    )

    async def locked_document(db, _document_id, _entity_id, *, mutation_authorizer, **_kwargs):
        await mutation_authorizer(db, document)
        return document

    async def capabilities(*_args, **_kwargs):
        return {Capability.VIEW, Capability.SHARE_INTERNAL}

    async def readable(*_args, **_kwargs):
        return True

    async def no_group(*_args, **_kwargs):
        return None

    async def no_cache(*_args, **_kwargs):
        return None

    class BindingDb(_FakeDb):
        async def flush(self):
            return None

    monkeypatch.setattr(document_service, "_get_document_for_policy_mutation", locked_document)
    monkeypatch.setattr(document_access, "effective_document_capabilities_for_user", capabilities)
    monkeypatch.setattr(document_access, "user_can_read_document", readable)
    monkeypatch.setattr(file_context, "_ensure_workspace_upload_group", no_group)
    monkeypatch.setattr(file_context, "bump_tool_cache_version", no_cache)

    refs = await file_context.bind_workspace_knowledge_documents(
        BindingDb([]),
        entity_id="entity_1",
        workspace_id="workspace_1",
        user_id="requester",
        attachments=[{"document_id": document.id}],
    )

    assert refs[0]["document_id"] == document.id
    assert document.visibility == "workspace"


@pytest.mark.asyncio
async def test_large_chat_image_is_saved_for_tools_but_not_inlined(monkeypatch):
    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_BYTES", 16)
    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_TOTAL_BYTES", 16)
    monkeypatch.setattr(
        file_context,
        "_save_chat_image",
        lambda content, filename, entity_id, mime: f"/api/v1/fs/{entity_id}/uploads/chat/{filename}",
    )

    content = b"\x89PNG\r\n\x1a\n" + b"x" * 32

    attachments = await file_context.build_file_context(
        [_upload("storm.png", content)],
        [],
        "entity_1",
        db=None,
    )

    assert attachments.image_blocks == []
    assert attachments.image_urls == ["/api/v1/fs/entity_1/uploads/chat/storm.png"]
    assert attachments.image_reference_lines == ["[Image: storm.png → /api/v1/fs/entity_1/uploads/chat/storm.png]"]
    assert attachments.attachment_refs == [
        {
            "kind": "chat_upload",
            "name": "storm.png",
            "mime": "image/png",
            "path": "uploads/chat/storm.png",
            "url": "/api/v1/fs/entity_1/uploads/chat/storm.png",
            "image": True,
        }
    ]
    assert "[Image: storm.png" in attachments.text_context
    assert "/api/v1/fs/entity_1/uploads/chat/storm.png" in attachments.text_context


@pytest.mark.asyncio
async def test_small_chat_image_is_saved_and_inlined(monkeypatch):
    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_BYTES", 128)
    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_TOTAL_BYTES", 128)
    monkeypatch.setattr(
        file_context,
        "_save_chat_image",
        lambda content, filename, entity_id, mime: f"/api/v1/fs/{entity_id}/uploads/chat/{filename}",
    )

    content = b"\x89PNG\r\n\x1a\n" + b"x" * 8

    attachments = await file_context.build_file_context(
        [_upload("tiny.png", content)],
        [],
        "entity_1",
        db=None,
    )

    assert attachments.image_urls == ["/api/v1/fs/entity_1/uploads/chat/tiny.png"]
    assert attachments.attachment_refs[0]["path"] == "uploads/chat/tiny.png"
    assert len(attachments.image_blocks) == 1
    assert attachments.image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_workspace_chat_image_is_registered_in_workspace_knowledge(monkeypatch):
    from packages.core.ai.runtime import ChatSurface

    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_BYTES", 128)
    monkeypatch.setattr(file_context, "_MAX_INLINE_IMAGE_TOTAL_BYTES", 128)
    monkeypatch.setattr(
        file_context,
        "_save_chat_image",
        lambda content, filename, entity_id, mime, **_kwargs: f"/api/v1/fs/{entity_id}/uploads/chat/{filename}",
    )
    registrations = []

    async def _register(db_arg, **kwargs):
        registrations.append({"db": db_arg, **kwargs})
        return "doc_image"

    monkeypatch.setattr(file_context, "_register_workspace_chat_upload", _register)
    db = object()
    content = b"\x89PNG\r\n\x1a\n" + b"x" * 8

    attachments = await file_context.build_file_context(
        [_upload("cover.png", content)],
        [],
        "entity_1",
        db=db,
        workspace_id="workspace_1",
        user_id="user_1",
        surface=ChatSurface.WORKSPACE_CHAT,
    )

    assert registrations == [{
        "db": db,
        "entity_id": "entity_1",
        "workspace_id": "workspace_1",
        "user_id": "user_1",
        "surface": ChatSurface.WORKSPACE_CHAT,
        "filename": "cover.png",
        "saved_rel_path": "uploads/chat/cover.png",
        "saved_url": "/api/v1/fs/entity_1/uploads/chat/cover.png",
        "file_size": len(content),
        "mime": "image/png",
    }]
    assert attachments.attachment_refs[0]["path"] == "uploads/chat/cover.png"


@pytest.mark.asyncio
async def test_kb_image_with_file_url_is_available_to_llm_and_tools():
    doc = Document(
        id="doc_remote_img",
        entity_id="entity_1",
        name="reference.png",
        fs_path=None,
        file_url="https://cdn.example.test/reference.png",
        file_type="png",
        mime_type="image/png",
        source="ai_generated",
    )

    attachments = await file_context.build_file_context(
        [],
        ["doc_remote_img"],
        "entity_1",
        db=_FakeDb([doc]),
    )

    assert attachments.image_urls == ["https://cdn.example.test/reference.png"]
    assert attachments.image_reference_lines == [
        "[Image from KB: reference.png → https://cdn.example.test/reference.png]"
    ]
    assert attachments.image_blocks == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example.test/reference.png"},
        },
    ]
    assert attachments.attachment_refs == [
        {
            "kind": "knowledge_document",
            "name": "reference.png",
            "mime": "image/png",
            "url": "https://cdn.example.test/reference.png",
            "document_id": "doc_remote_img",
            "image": True,
        }
    ]
    assert "[Image from KB: reference.png" in attachments.text_context


@pytest.mark.asyncio
async def test_kb_document_is_unavailable_when_its_entity_is_missing():
    doc = Document(
        id="doc_missing_entity",
        entity_id="entity_1",
        name="reference.png",
        file_url="https://cdn.example.test/reference.png",
        file_type="png",
        mime_type="image/png",
        source="ai_generated",
    )

    attachments = await file_context.build_file_context(
        [],
        ["doc_missing_entity"],
        "entity_1",
        db=_FakeDb([doc], entity_available=False),
    )

    assert attachments.attachment_refs == []
    assert attachments.text_context == ""


@pytest.mark.asyncio
async def test_kb_video_is_available_as_structured_reference():
    doc = Document(
        id="doc_video",
        entity_id="entity_1",
        name="motion.mp4",
        fs_path="videos/motion.mp4",
        file_type="mp4",
        mime_type="video/mp4",
        source="ai_generated",
    )

    attachments = await file_context.build_file_context(
        [],
        ["doc_video"],
        "entity_1",
        db=_FakeDb([doc]),
    )

    assert attachments.video_urls == ["/api/v1/fs/entity_1/videos/motion.mp4"]
    assert attachments.attachment_refs == [
        {
            "kind": "knowledge_document",
            "name": "motion.mp4",
            "mime": "video/mp4",
            "path": "videos/motion.mp4",
            "url": "/api/v1/fs/entity_1/videos/motion.mp4",
            "document_id": "doc_video",
            "video": True,
        }
    ]
    assert "[Video from KB: motion.mp4" in attachments.text_context


@pytest.mark.asyncio
async def test_kb_mp4_octet_stream_is_available_as_video_reference():
    doc = Document(
        id="doc_octet_video",
        entity_id="entity_1",
        name="sketch-dark-bg-animation-3.mp4",
        fs_path="videos/sketch-dark-bg-animation-3.mp4",
        file_type="mp4",
        mime_type="application/octet-stream",
        source="ai_generated",
    )

    attachments = await file_context.build_file_context(
        [],
        ["doc_octet_video"],
        "entity_1",
        db=_FakeDb([doc]),
    )

    assert attachments.video_urls == ["/api/v1/fs/entity_1/videos/sketch-dark-bg-animation-3.mp4"]
    assert attachments.attachment_refs == [
        {
            "kind": "knowledge_document",
            "name": "sketch-dark-bg-animation-3.mp4",
            "mime": "application/octet-stream",
            "path": "videos/sketch-dark-bg-animation-3.mp4",
            "url": "/api/v1/fs/entity_1/videos/sketch-dark-bg-animation-3.mp4",
            "document_id": "doc_octet_video",
            "video": True,
        }
    ]
    assert "[Video from KB: sketch-dark-bg-animation-3.mp4" in attachments.text_context


@pytest.mark.asyncio
async def test_kb_text_document_uses_legacy_content_text_metadata():
    doc = Document(
        id="doc_legacy_text",
        entity_id="entity_1",
        name="starter.md",
        fs_path=None,
        file_type="md",
        mime_type="text/markdown",
        source="ai_generated",
    )
    doc.metadata_ = {"content_text": "# Starter\n\nLegacy inline body."}

    attachments = await file_context.build_file_context(
        [],
        ["doc_legacy_text"],
        "entity_1",
        db=_FakeDb([doc]),
    )

    assert attachments.unread_filenames == []
    assert attachments.attachment_refs[0]["kind"] == "knowledge_document"
    assert attachments.attachment_refs[0]["document_id"] == "doc_legacy_text"
    assert attachments.to_runtime_context()["counts"]["refs"] == 1
    assert "# Starter" in attachments.text_context
    assert "Legacy inline body." in attachments.text_context


@pytest.mark.asyncio
async def test_kb_text_document_with_fs_path_stores_reference_even_without_text(monkeypatch):
    async def _empty_extract(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(file_context, "extract_text", _empty_extract)
    doc = Document(
        id="doc_attached_template",
        entity_id="entity_1",
        name="completion-reviews/daily-review-template.md",
        fs_path="completion-reviews/daily-review-template.md",
        file_type="md",
        mime_type="text/markdown",
        source="upload",
    )

    attachments = await file_context.build_file_context(
        [],
        ["doc_attached_template"],
        "entity_1",
        db=_FakeDb([doc]),
    )

    assert attachments.attachment_refs == [
        {
            "kind": "knowledge_document",
            "name": "completion-reviews/daily-review-template.md",
            "mime": "text/markdown",
            "path": "completion-reviews/daily-review-template.md",
            "url": "/api/v1/fs/entity_1/completion-reviews/daily-review-template.md",
            "document_id": "doc_attached_template",
            "text": True,
        }
    ]
    assert "KB: completion-reviews/daily-review-template.md" in attachments.text_context
