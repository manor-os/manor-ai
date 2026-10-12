"""Ordinary Chat output filing in Knowledge."""

from __future__ import annotations

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.task import Conversation
from packages.core.services.conversation_generated_files import (
    ensure_conversation_generated_folder,
    is_default_chat_output_path,
)
from packages.core.services.conversation_lifecycle import rename_conversation
from packages.core.services.generated_artifact_service import (
    generated_artifact_document_folder_id,
)
from packages.core.services.knowledge_sync import sync_file_to_knowledge


def test_chat_default_paths_leave_explicit_destinations_alone():
    assert is_default_chat_output_path("brief.md")
    assert is_default_chat_output_path("videos/ad.mp4")
    assert is_default_chat_output_path("documents/brief.pdf")
    assert not is_default_chat_output_path("Campaign/brief.pdf")
    assert not is_default_chat_output_path("Campaign/videos/ad.mp4")


@pytest.mark.asyncio
async def test_chat_output_folder_follows_title_and_keeps_file_identity(
    db_session, monkeypatch, tmp_path,
):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")

    entity_id, user_id = generate_ulid(), generate_ulid()
    first = Conversation(
        id=generate_ulid(), entity_id=entity_id, user_id=user_id,
        title="Thanksgiving / Product Ad", channel="web", scope="channel",
    )
    second = Conversation(
        id=generate_ulid(), entity_id=entity_id, user_id=user_id,
        title=first.title, channel="web", scope="channel",
    )
    workspace_chat = Conversation(
        id=generate_ulid(), entity_id=entity_id, user_id=user_id,
        workspace_id=generate_ulid(), title="Workspace", channel="web",
        scope="workspace_main",
    )
    edit_chat = Conversation(
        id=generate_ulid(), entity_id=entity_id, user_id=user_id,
        title="Editor", channel="web", scope="channel",
        meta={"surface": "ai_edit"},
    )
    db_session.add_all((first, second, workspace_chat, edit_chat))
    await db_session.commit()

    folder_id = await ensure_conversation_generated_folder(
        db_session, entity_id=entity_id, user_id=user_id, conversation_id=first.id,
    )
    duplicate_id = await ensure_conversation_generated_folder(
        db_session, entity_id=entity_id, user_id=user_id, conversation_id=second.id,
    )
    assert folder_id and duplicate_id and folder_id != duplicate_id
    assert (await db_session.get(DocumentFolder, folder_id)).name == "Thanksgiving Product Ad"
    assert (await db_session.get(DocumentFolder, duplicate_id)).name.startswith("Thanksgiving Product Ad · ")
    assert await ensure_conversation_generated_folder(
        db_session, entity_id=entity_id, user_id=user_id, conversation_id=workspace_chat.id,
    ) is None
    assert await ensure_conversation_generated_folder(
        db_session, entity_id=entity_id, user_id=user_id, conversation_id=edit_chat.id,
    ) is None
    assert await ensure_conversation_generated_folder(
        db_session, entity_id=entity_id, user_id=generate_ulid(), conversation_id=first.id,
    ) is None
    assert await generated_artifact_document_folder_id(
        entity_id=entity_id, user_id=user_id, workspace_id=None,
        rel_path="videos/thanksgiving-ad.mp4", conversation_id=first.id,
        db=db_session,
    ) == folder_id

    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    output = entity_root / "thanksgiving-ad.md"
    output.write_text("# Product ad", encoding="utf-8")
    projected = await sync_file_to_knowledge(
        entity_id=entity_id, abs_path=str(output), entity_root=str(entity_root),
        source="ai_generated", created_by=user_id, user_id=user_id,
        conversation_id=first.id, force=True, db=db_session, commit=True,
    )
    assert projected.synced and projected.document_id
    document = await db_session.get(Document, projected.document_id)
    assert document.folder_id == folder_id
    assert document.fs_path == "thanksgiving-ad.md"

    custom_dir = entity_root / "Campaign"
    custom_dir.mkdir()
    custom_output = custom_dir / "brief.md"
    custom_output.write_text("# Brief", encoding="utf-8")
    custom = await sync_file_to_knowledge(
        entity_id=entity_id, abs_path=str(custom_output), entity_root=str(entity_root),
        source="ai_generated", created_by=user_id, user_id=user_id,
        conversation_id=first.id, force=True, db=db_session, commit=True,
    )
    assert custom.synced and custom.document_id
    custom_document = await db_session.get(Document, custom.document_id)
    assert custom_document.folder_id != folder_id
    assert (await db_session.get(DocumentFolder, custom_document.folder_id)).name == "Campaign"

    existing = entity_root / "existing.md"
    existing.write_text("Existing file edited in Chat", encoding="utf-8")
    patched = await sync_file_to_knowledge(
        entity_id=entity_id, abs_path=str(existing), entity_root=str(entity_root),
        source="agent", tool_name="patch_file", created_by=user_id,
        user_id=user_id, conversation_id=first.id, force=True,
        db=db_session, commit=True,
    )
    assert patched.synced and patched.document_id
    assert (await db_session.get(Document, patched.document_id)).folder_id is None
    existing.write_text("Updated with generate_file", encoding="utf-8")
    regenerated = await sync_file_to_knowledge(
        entity_id=entity_id, abs_path=str(existing), entity_root=str(entity_root),
        source="ai_generated", tool_name="generate_file", created_by=user_id,
        user_id=user_id, conversation_id=first.id, force=True,
        db=db_session, commit=True,
    )
    assert regenerated.document_id == patched.document_id
    assert (await db_session.get(Document, regenerated.document_id)).folder_id is None

    await rename_conversation(
        db_session, first.id, entity_id, "Holiday Tea Machine Campaign",
    )
    await db_session.commit()
    assert (await db_session.get(DocumentFolder, folder_id)).name == "Holiday Tea Machine Campaign"
    assert (await db_session.get(Document, projected.document_id)).folder_id == folder_id
