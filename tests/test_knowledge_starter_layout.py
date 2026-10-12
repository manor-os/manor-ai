from __future__ import annotations

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.knowledge_starter import write_markdown_document


@pytest.fixture(autouse=True)
def entity_filesystem(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))


@pytest.mark.asyncio
async def test_workspace_starter_uses_workspace_root_and_basename(db_session, tmp_path):
    entity_id, workspace_id = generate_ulid(), generate_ulid()
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Launch")
    db_session.add(workspace)
    await db_session.flush()

    document = await write_markdown_document(
        db_session,
        entity_id=entity_id,
        name="nested/Starter Brief",
        content="# Brief",
        source="workspace_setup",
        metadata={"origin": {"workspace_id": workspace_id}},
    )

    await db_session.refresh(workspace)
    assert document.name == "Starter Brief.md"
    assert document.folder_id == workspace.artifact_folder_id
    assert document.fs_path == f"Workspaces/_by_id/{workspace.artifact_folder_id}/Starter Brief.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Brief"

    folders = list((await db_session.scalars(
        select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
    )).all())
    assert {folder.name for folder in folders} == {"Workspaces", "Launch"}


@pytest.mark.asyncio
async def test_entity_starter_without_workspace_uses_entity_root(db_session, tmp_path):
    entity_id = generate_ulid()

    document = await write_markdown_document(
        db_session,
        entity_id=entity_id,
        name="Entity Brief",
        content="# Entity",
        source="workspace_setup",
        metadata={"origin": {}},
    )

    assert document.folder_id is None
    assert document.fs_path == "Entity Brief.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Entity"


@pytest.mark.asyncio
async def test_unknown_workspace_starter_never_falls_back_to_entity_root(db_session, tmp_path):
    entity_id = generate_ulid()
    filename = "Unknown Workspace.md"

    with pytest.raises(ValueError, match="Workspace"):
        await write_markdown_document(
            db_session,
            entity_id=entity_id,
            name=filename,
            content="# Invalid",
            source="workspace_setup",
            metadata={"origin": {"workspace_id": generate_ulid()}},
        )

    assert not (tmp_path / entity_id / filename).exists()
    assert (await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id)
    )).all() == []
