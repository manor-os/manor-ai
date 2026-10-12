"""Signed document content cannot be replaced through either save service."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.config import get_settings
from packages.core.services import document_service, entity_fs, version_service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("save_kind", "filesystem_enabled"),
    [("text", True), ("text", False), ("binary", True)],
)
async def test_completed_signature_rejects_content_replacement(
    monkeypatch, tmp_path, save_kind, filesystem_enabled,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", filesystem_enabled)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    metadata = {
        "signature_lock": {"status": "completed", "document_sha256": "signed-digest"},
    }
    doc = SimpleNamespace(
        id="signed-document",
        entity_id="entity",
        name="agreement.pdf",
        file_type="pdf",
        mime_type="application/pdf",
        fs_path="agreement.pdf" if filesystem_enabled else None,
        metadata_=metadata,
    )
    load = AsyncMock(return_value=doc)
    monkeypatch.setattr(document_service, "get_document", load)

    @asynccontextmanager
    async def filesystem_boundary(_root):
        yield

    monkeypatch.setattr(entity_fs, "entity_filesystem_mutation_lock", filesystem_boundary)
    monkeypatch.setattr(entity_fs, "assert_entity_filesystem_ready", lambda: None)
    write = Mock()
    create_version = AsyncMock()
    monkeypatch.setattr(entity_fs, "write_entity_file_atomic", write)
    monkeypatch.setattr(version_service, "create_version", create_version)
    db = AsyncMock()

    with pytest.raises(document_service.DocumentMutationConflictError, match="Signed PDFs are immutable"):
        if save_kind == "text":
            await document_service.save_document_content(
                db, doc.id, doc.entity_id, "replacement contract",
            )
        else:
            await document_service.save_document_file(
                db, doc.id, doc.entity_id, b"replacement contract",
            )

    load.assert_awaited_once_with(
        db, doc.id, doc.entity_id,
        for_filesystem_mutation=True,
        mutation_authorizer=None,
    )
    write.assert_not_called()
    create_version.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert doc.metadata_ == metadata
