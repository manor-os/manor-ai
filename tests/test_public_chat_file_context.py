"""Public inline document references must use customer visibility before reading bytes."""
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from starlette.datastructures import Headers, UploadFile

from packages.core.ai.runtime import ChatSurface
from packages.core.models.document import Document
from packages.core.services import document_access, document_service, file_context
from packages.core.services.runtime_file_context import prepare_runtime_file_context_turn


@pytest.mark.parametrize("user_id", [None, "owner"])
@pytest.mark.parametrize("policy", ["private", "confidential", "restricted", "not_client_visible", "other_workspace", "other_entity", "deleted", "public"])
async def test_public_document_references_do_not_inherit_internal_access(monkeypatch, user_id, policy):
    document = Document(
        id="document", entity_id="other" if policy == "other_entity" else "entity",
        name="Note", visibility="private" if policy == "private" else "workspace",
        classification=policy if policy in {"confidential", "restricted"} else "internal",
        client_visible=policy != "not_client_visible", owner_id="owner",
        file_type="txt", mime_type="text/plain", metadata_={"content": "SYNTHETIC_DOCUMENT_CONTENT"},
    )
    monkeypatch.setattr(document_service, "get_document", AsyncMock(return_value=document))
    monkeypatch.setattr(document_access, "document_is_owned_by_deleted_workspace", AsyncMock(return_value=policy == "deleted"))
    monkeypatch.setattr(document_access, "effective_document_folder_policy", AsyncMock(return_value=(
        document.classification, document.visibility, document.client_visible,
    )))
    monkeypatch.setattr(document_access, "document_workspace_ids", AsyncMock(return_value={
        "other-workspace" if policy == "other_workspace" else "workspace",
    }))
    # An accidental return to the generic ACL would grant these callers access.
    monkeypatch.setattr(document_access, "get_visible_document", AsyncMock(return_value=document))

    turn = await prepare_runtime_file_context_turn(
        message="Summarize #[note](doc:document)", document_ids=[], files=[],
        entity_id="entity", db=AsyncMock(), workspace_id="workspace", user_id=user_id,
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
    )

    assert ("SYNTHETIC_DOCUMENT_CONTENT" in turn.attachments.text_context) == (policy == "public")
    assert bool(turn.attachments.attachment_refs) == (policy == "public")
    document_access.get_visible_document.assert_not_awaited()


async def test_public_visitor_upload_remains_available(monkeypatch):
    monkeypatch.setattr(file_context, "_save_chat_image", lambda *_: "/api/v1/fs/entity/uploads/chat/image.png")
    upload = UploadFile(
        filename="image.png", file=BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * 8),
        headers=Headers({"content-type": "image/png"}),
    )
    turn = await prepare_runtime_file_context_turn(
        message="Look at this", document_ids=[], files=[upload], entity_id="entity",
        db=AsyncMock(), workspace_id="workspace", user_id=None,
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
    )
    assert len(turn.attachments.image_blocks) == 1
    assert turn.attachments.attachment_refs[0]["kind"] == "chat_upload"
