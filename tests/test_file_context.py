from __future__ import annotations

from io import BytesIO

import pytest
from starlette.datastructures import Headers, UploadFile

from packages.core.models.document import Document
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
    def __init__(self, docs):
        self._docs = docs

    async def execute(self, _stmt):
        return _FakeResult(self._docs)


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
