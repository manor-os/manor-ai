"""Social media sources retain their actor scope and exact approved bytes."""

import hashlib
from pathlib import Path

import pytest

from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.services.chat_attachment_media import inspect_chat_attachment_media
from packages.core.services.file_access_tokens import create_file_access_token
from packages.core.tasks import media_tasks
from test_chat_attachment_read_access import attachment_fs as attachment_fs, seed_attachment


@pytest.mark.asyncio
async def test_scoped_video_url_keeps_original_claims_and_expiry(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    token = create_file_access_token(
        entity_id="entity-1",
        rel_path="uploads/chat/1234567890abcdef.mp4",
        user_id="user-1",
        conversation_id="conversation-1",
        source_sha256="a" * 64,
    )
    url = f"https://app.example.test/api/v1/fs/public/{token}/video.mp4"

    def no_remint(**_kwargs):
        pytest.fail("Scoped attachment token must not become a legacy bearer token")

    monkeypatch.setattr("packages.core.services.file_access_tokens.create_file_access_token", no_remint)
    assert media_tasks._entity_rel_path_from_reference(url, "entity-1", str(tmp_path / "entity-1")) is None
    assert await media_tasks._ensure_public_url(url, "entity-1", allow_data_uri=False) == url
    assert await media_tasks._ensure_inline_data_url(url, "entity-1", max_bytes=100) == url


def test_legacy_media_token_still_rehydrates_to_its_original_file(tmp_path):
    path = "uploads/chat/1234567890abcdef.mp4"
    token = create_file_access_token(entity_id="entity-1", rel_path=path)
    url = f"https://app.example.test/api/v1/fs/public/{token}/video.mp4"
    assert media_tasks._entity_rel_path_from_reference(url, "entity-1", str(tmp_path / "entity-1")) == path


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("workspace", [False, True])
async def test_social_attachment_inspection_keeps_current_conversation_and_bytes(db_session, attachment_fs, workspace):
    user, conversation, _message, ws, source, path = await seed_attachment(
        db_session, attachment_fs, workspace=workspace, extension="jpg",
    )
    inspected = []

    def inspect_snapshot(descriptor_path):
        data = Path(descriptor_path).read_bytes()
        inspected.append(data)
        return {"inspected_size": len(data)}

    result = await inspect_chat_attachment_media(
        {"source_kind": "chat_attachment", "path": path},
        entity_id=user.entity_id, user_id=user.id, conversation_id=conversation.id,
        workspace_id=ws.id if ws else None, tool_name="mcp__facebook__create_instagram_media",
        allowed_suffixes={".jpg"}, max_bytes=1024, inspect_snapshot=inspect_snapshot,
    )
    expected = source.read_bytes()
    assert inspected == [expected]
    assert result == {
        "source_kind": "chat_attachment", "rel_path": path,
        "source_sha256": hashlib.sha256(expected).hexdigest(),
        "size_bytes": len(expected), "inspected_size": len(expected),
    }


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("restriction", ["other_user", "other_conversation", "trashed_document", "removed_attachment"])
async def test_social_attachment_inspection_revalidates_scope_before_read(db_session, attachment_fs, restriction):
    user, conversation, message, _ws, _source, path = await seed_attachment(
        db_session, attachment_fs, extension="jpg",
    )
    context = dict(
        entity_id=user.entity_id, user_id=user.id, conversation_id=conversation.id,
        workspace_id=None, tool_name="mcp__facebook__create_instagram_media",
        allowed_suffixes={".jpg"}, max_bytes=1024,
    )
    reference = {"source_kind": "chat_attachment", "path": path}
    assert (await inspect_chat_attachment_media(reference, **context))["rel_path"] == path

    if restriction == "other_user":
        other_id = generate_ulid()
        db_session.add(User(
            id=other_id, entity_id=user.entity_id, email=f"{other_id}@test.com",
            password_hash="unused", role="owner", status="active",
        ))
        context["user_id"] = other_id
    elif restriction == "other_conversation":
        other = Conversation(entity_id=user.entity_id, user_id=user.id, scope="channel", status="active")
        db_session.add(other)
        await db_session.flush()
        context["conversation_id"] = other.id
    elif restriction == "trashed_document":
        db_session.add(Document(
            entity_id=user.entity_id, name="attachment.jpg", fs_path=path, file_type="jpg",
            owner_id=user.id, visibility="private", source="chat_upload", is_trashed=True,
        ))
    else:
        message.attachments = None
    await db_session.commit()

    def must_not_read(_descriptor_path):
        pytest.fail("A revoked or differently scoped attachment reached media inspection")

    with pytest.raises(ValueError, match="no longer readable"):
        await inspect_chat_attachment_media(reference, **context, inspect_snapshot=must_not_read)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_social_attachment_inspection_rejects_source_replacement(db_session, attachment_fs):
    user, conversation, _message, _ws, source, path = await seed_attachment(
        db_session, attachment_fs, extension="jpg",
    )
    approved_bytes = source.read_bytes()
    inspected = []

    def replace_during_inspection(descriptor_path):
        source.rename(source.with_suffix(".original"))
        source.write_bytes(b"different bytes after authorization")
        inspected.append(Path(descriptor_path).read_bytes())
        return {}

    with pytest.raises(ValueError, match="unavailable or has changed"):
        await inspect_chat_attachment_media(
            {"source_kind": "chat_attachment", "path": path},
            entity_id=user.entity_id, user_id=user.id, conversation_id=conversation.id,
            workspace_id=None, tool_name="mcp__facebook__create_instagram_media",
            allowed_suffixes={".jpg"}, max_bytes=1024, inspect_snapshot=replace_during_inspection,
        )
    assert inspected == [approved_bytes]
