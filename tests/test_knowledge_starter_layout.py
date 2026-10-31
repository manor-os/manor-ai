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


@pytest.mark.asyncio
async def test_generated_workspace_starter_stays_in_workspace_storage(
    db_session, monkeypatch, tmp_path,
):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.ai.runtime as runtime
    import packages.core.database as database
    from packages.core.models.document import DocumentGroup
    from packages.core.tasks import ai_tasks

    entity_id, workspace_id, group_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Studio"))
    db_session.add(DocumentGroup(id=group_id, entity_id=entity_id, name="Brand"))
    await db_session.commit()

    async def completion(**_kwargs):
        return SimpleNamespace(content="# Brand guide")

    pending = []
    monkeypatch.setattr(
        runtime, "runtime_execute_knowledge_starter_document_completion", completion,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False,
    ))
    monkeypatch.setattr(ai_tasks, "_run_async", pending.append)
    monkeypatch.setattr(ai_tasks.process_document_embeddings, "delay", lambda *_: None)

    ai_tasks.generate_knowledge_content.run(
        entity_id, workspace_id, group_id, "Brand", "purpose",
        "Studio", "studio", "videos",
    )
    await pending[0]

    db_session.expire_all()
    document = (await db_session.scalars(
        select(Document).where(Document.entity_id == entity_id)
    )).one()
    workspace = await db_session.get(Workspace, workspace_id)
    assert document.folder_id == workspace.artifact_folder_id
    assert document.fs_path == f"Workspaces/_by_id/{workspace.artifact_folder_id}/Brand.md"
    assert (tmp_path / entity_id / document.fs_path).read_text(encoding="utf-8") == "# Brand guide"
    assert not (tmp_path / entity_id / "Brand.md").exists()
