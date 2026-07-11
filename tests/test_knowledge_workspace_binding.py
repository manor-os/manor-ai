from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from httpx import AsyncClient

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import Workspace
from packages.core.services import knowledge_sync


@pytest.mark.asyncio
async def test_sync_file_to_knowledge_marks_generated_file_workspace_provenance(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    existing_group_id = generate_ulid()
    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    report = entity_root / "report.md"
    report.write_text("# Report\n\nWorkspace scoped output.", encoding="utf-8")

    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Launch Workspace"))
    db_session.add(
        DocumentGroup(
            id=existing_group_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            name="Product Knowledge",
            settings={"purpose": "Domain knowledge, not generated file output."},
        )
    )
    await db_session.commit()

    result = await knowledge_sync.sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=str(report),
        entity_root=str(entity_root),
        source="agent",
        created_by="ai-agent",
        force=True,
        workspace_id=workspace_id,
        task_id="TASK123",
    )

    assert result.synced is True
    assert result.document_id

    doc = await db_session.get(Document, result.document_id)
    assert doc is not None
    assert doc.metadata_["schema_version"] == 2
    assert doc.metadata_["origin"]["workspace_id"] == workspace_id
    assert doc.metadata_["origin"]["task_id"] == "TASK123"
    assert doc.metadata_["artifact"]["role"] == "final"
    assert "workspace_ids" not in doc.metadata_
    assert "generated_in_workspace" not in doc.metadata_

    groups = list(
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    assert not any((g.settings or {}).get("workspace_file_bucket") for g in groups)

    existing_member = (
        await db_session.execute(
            select(DocumentGroupMember).where(
                DocumentGroupMember.document_id == result.document_id,
                DocumentGroupMember.group_id == existing_group_id,
            )
        )
    ).scalar_one_or_none()
    assert existing_member is None


@pytest.mark.asyncio
async def test_sync_file_to_knowledge_does_not_bind_unknown_workspace(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)

    entity_id = generate_ulid()
    entity_root = tmp_path / entity_id
    entity_root.mkdir()
    report = entity_root / "report.md"
    report.write_text("# Report", encoding="utf-8")

    result = await knowledge_sync.sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=str(report),
        entity_root=str(entity_root),
        source="agent",
        force=True,
        workspace_id=generate_ulid(),
    )

    assert result.synced is True
    assert not (await db_session.execute(select(DocumentGroup))).scalars().all()


@pytest.mark.asyncio
async def test_sync_file_to_knowledge_preserves_code_type_and_folder(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)

    entity_id = generate_ulid()
    entity_root = tmp_path / entity_id
    bundle_dir = entity_root / "code" / "demo-site"
    bundle_dir.mkdir(parents=True)
    app = bundle_dir / "app.js"
    app.write_text("console.log('ready');\n", encoding="utf-8")

    result = await knowledge_sync.sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=str(app),
        entity_root=str(entity_root),
        source="ai_generated",
        force=True,
    )

    assert result.synced is True
    doc = await db_session.get(Document, result.document_id)
    assert doc is not None
    assert doc.name == "app.js"
    assert doc.file_type == "js"
    assert doc.mime_type == "text/javascript"

    folders = list(
        (await db_session.execute(select(DocumentFolder).where(DocumentFolder.entity_id == entity_id))).scalars().all()
    )
    folder_by_name = {folder.name: folder for folder in folders}
    assert sorted(folder_by_name) == ["code", "demo-site"]
    assert doc.folder_id == folder_by_name["demo-site"].id
    assert folder_by_name["demo-site"].parent_id == folder_by_name["code"].id


@pytest.mark.asyncio
async def test_reconcile_entity_filesystem_syncs_real_paths_and_trashes_missing(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)

    entity_id = generate_ulid()
    entity_root = tmp_path / entity_id
    (entity_root / "NewFolder").mkdir(parents=True)
    (entity_root / "NewFolder" / "brief.md").write_text("# Brief\n\nMoved.", encoding="utf-8")
    (entity_root / "slide_01.svg").write_text("<svg></svg>", encoding="utf-8")
    (entity_root / "svg_output").mkdir()
    (entity_root / "svg_output" / "slide_01.svg").write_text("<svg></svg>", encoding="utf-8")

    stale_id = generate_ulid()
    db_session.add(
        Document(
            id=stale_id,
            entity_id=entity_id,
            name="brief.md",
            fs_path="OldFolder/brief.md",
            file_type="md",
            source="upload",
            vector_status="ready",
        )
    )
    await db_session.commit()

    result = await knowledge_sync.reconcile_entity_filesystem(
        entity_id=entity_id,
        entity_root=str(entity_root),
        source="filesystem_reconcile",
        created_by="test",
    )

    assert result.scanned_files == 1
    assert result.synced_files == 1
    assert result.trashed_missing_documents == 1

    async with db_module.async_session() as db:
        stale = await db.get(Document, stale_id)
        assert stale is not None
        assert stale.is_trashed is True
        assert stale.vector_status == "failed"
        assert stale.metadata_["file_integrity"]["status"] == "missing"

        synced = (
            await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path == "NewFolder/brief.md",
                    Document.is_trashed == False,  # noqa: E712
                )
            )
        ).scalar_one_or_none()
        assert synced is not None
        assert synced.vector_status == "pending"
        assert synced.metadata_["file_integrity"]["status"] == "ok"
        assert synced.metadata_["file_integrity"]["mtime_ns"] is not None

        leaked_svg = (
            await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path == "svg_output/slide_01.svg",
                )
            )
        ).scalar_one_or_none()
        assert leaked_svg is None

        leaked_root_svg = (
            await db.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.fs_path == "slide_01.svg",
                )
            )
        ).scalar_one_or_none()
        assert leaked_root_svg is None


@pytest.mark.asyncio
async def test_ensure_folder_path_is_concurrency_safe(client: AsyncClient, db_session, monkeypatch):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)

    entity_id = generate_ulid()
    results = await asyncio.gather(
        *[knowledge_sync.ensure_folder_path(entity_id, "Project/keyframes") for _ in range(12)]
    )

    assert len(set(results)) == 1
    folders = list(
        (await db_session.execute(select(DocumentFolder).where(DocumentFolder.entity_id == entity_id))).scalars().all()
    )
    assert sorted(folder.name for folder in folders) == ["Project", "keyframes"]
