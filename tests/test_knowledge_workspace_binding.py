from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, DocumentGroup, DocumentGroupMember
from packages.core.models.user import Entity
from packages.core.models.workspace import Workspace
from packages.core.services import artifact_knowledge, knowledge_sync


@pytest.fixture(autouse=True)
def canonical_entity_fs_root(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")


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
async def test_task_projection_backfills_existing_document_provenance(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)
    monkeypatch.setattr(artifact_knowledge, "get_entity_root", lambda _entity_id: str(tmp_path))

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    task_id = generate_ulid()
    document_id = generate_ulid()
    db_session.add(Workspace(id=workspace_id, entity_id=entity_id, name="Launch Workspace"))
    document = Document(
        id=document_id,
        entity_id=entity_id,
        name="report.csv",
        fs_path="Workspaces/Demo/report.csv",
        source="agent",
        metadata_={"origin": {"workspace_id": workspace_id}},
    )
    db_session.add(document)
    await db_session.commit()

    projection = await artifact_knowledge.project_artifact_refs_to_knowledge(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        refs=[{
            "type": "file",
            "name": "report.csv",
            "document_id": document_id,
            "url": f"/viewer/{document_id}",
        }],
    )

    await db_session.refresh(document)
    assert projection.refs[0]["document_id"] == document_id
    assert projection.refs[0]["viewer_url"] == f"/viewer/{document_id}"
    assert document.metadata_["origin"]["workspace_id"] == workspace_id
    assert document.metadata_["origin"]["task_id"] == task_id


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
async def test_reconcile_preserves_existing_logical_folder_for_workspace_storage(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    import packages.core.database as db_module
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    monkeypatch.setattr(knowledge_sync, "async_session", db_module.async_session)
    entity_id = generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Projection")
    db_session.add(workspace)
    await db_session.flush()
    root = await ensure_workspace_artifact_folder(db_session, workspace)
    selected = DocumentFolder(entity_id=entity_id, name="Selected", parent_id=root.id)
    db_session.add(selected)
    await db_session.flush()
    rel_path = f"Workspaces/_by_id/{root.id}/tasks/run-1/audio/clip.mp3"
    file_path = tmp_path / entity_id / rel_path
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"audio")
    document = Document(
        entity_id=entity_id,
        name="clip.mp3",
        fs_path=rel_path,
        folder_id=selected.id,
        metadata_={"origin": {"workspace_id": workspace.id}},
    )
    db_session.add(document)
    await db_session.commit()

    result = await knowledge_sync.sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=str(file_path),
        entity_root=str(tmp_path / entity_id),
        source="filesystem_reconcile",
        force=True,
        visibility="private",
    )

    assert result.synced is True
    await db_session.refresh(document)
    saved = await db_session.get(Document, document.id)
    assert saved.folder_id == selected.id
    assert saved.visibility == "workspace"


@pytest.mark.asyncio
async def test_repair_workspace_projection_files_unique_legacy_documents_only(
    client: AsyncClient,
    db_session,
):
    from packages.core.services.knowledge_sync import repair_workspace_document_projection

    entity_id = generate_ulid()
    workspaces = [Workspace(entity_id=entity_id, name=name) for name in ("One", "Two")]
    db_session.add_all(workspaces)
    await db_session.flush()
    groups = [DocumentGroup(entity_id=entity_id, workspace_id=workspace.id, name=workspace.name) for workspace in workspaces]
    legacy = Document(
        entity_id=entity_id,
        name="brand/guide.md",
        fs_path="legacy/guide.md",
        metadata_={"origin": {"workspace_id": workspaces[0].id}},
    )
    shared = Document(
        entity_id=entity_id,
        name="shared/guide.md",
        fs_path="legacy/shared.md",
        metadata_={"origin": {"workspace_id": workspaces[0].id}},
    )
    db_session.add_all([*groups, legacy, shared])
    await db_session.flush()
    db_session.add_all([
        DocumentGroupMember(document_id=shared.id, group_id=groups[0].id),
        DocumentGroupMember(document_id=shared.id, group_id=groups[1].id),
    ])
    await db_session.flush()

    report = await repair_workspace_document_projection(
        db_session, entity_id=entity_id, workspace_id=workspaces[0].id,
    )

    assert report.repaired_documents == 1
    assert report.skipped_shared_documents == 1
    assert legacy.name == "guide.md"
    assert legacy.fs_path == "legacy/guide.md"
    assert legacy.folder_id is not None
    assert shared.name == "shared/guide.md"
    assert shared.fs_path == "legacy/shared.md"
    assert shared.folder_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_user_folder", "has_foreign_origin"),
    [(True, False), (False, True)],
)
async def test_need_input_binding_preserves_existing_folder_or_foreign_origin(
    client: AsyncClient,
    db_session,
    monkeypatch,
    has_user_folder: bool,
    has_foreign_origin: bool,
):
    from packages.core.models.permission import Capability
    from packages.core.services import document_access
    from packages.core.services.file_context import bind_workspace_knowledge_documents

    async def allowed_capabilities(*_args, **_kwargs):
        return {Capability.VIEW, Capability.SHARE_INTERNAL}

    async def readable(*_args, **_kwargs):
        return True

    monkeypatch.setattr(document_access, "effective_document_capabilities_for_user", allowed_capabilities)
    monkeypatch.setattr(document_access, "user_can_read_document", readable)

    entity_id = generate_ulid()
    entity = Entity(id=entity_id, name="Knowledge binding entity")
    target_workspace = Workspace(entity_id=entity_id, name="Target")
    other_workspace = Workspace(entity_id=entity_id, name="Other")
    user_folder = DocumentFolder(entity_id=entity_id, name="User uploads")
    db_session.add_all([entity, target_workspace, other_workspace, user_folder])
    await db_session.flush()
    original_origin = {
        "workspace_id": other_workspace.id,
        "tool_name": "existing_workspace_upload",
    } if has_foreign_origin else {"user_id": generate_ulid(), "tool_name": "upload"}
    document = Document(
        entity_id=entity_id,
        name="brief.md",
        fs_path="User uploads/brief.md",
        folder_id=user_folder.id if has_user_folder else None,
        visibility="private",
        metadata_={"origin": original_origin},
    )
    db_session.add(document)
    await db_session.flush()

    bound = await bind_workspace_knowledge_documents(
        db_session,
        entity_id=entity_id,
        workspace_id=target_workspace.id,
        user_id=generate_ulid(),
        attachments=[{"document_id": document.id}],
    )

    assert bound[0]["document_id"] == document.id
    assert document.folder_id == (user_folder.id if has_user_folder else None)
    assert document.fs_path == "User uploads/brief.md"
    assert document.metadata_["origin"] == original_origin
    assert document.visibility == "private"
    memberships = list((await db_session.scalars(
        select(DocumentGroupMember.group_id).where(
            DocumentGroupMember.document_id == document.id,
        )
    )).all())
    assert memberships
    groups = list((await db_session.scalars(
        select(DocumentGroup).where(DocumentGroup.id.in_(memberships))
    )).all())
    assert any(group.workspace_id == target_workspace.id for group in groups)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_state", ["missing", "deleted"])
async def test_need_input_binding_does_not_publish_from_unavailable_entity(
    client: AsyncClient,
    db_session,
    entity_state: str,
):
    from datetime import UTC, datetime

    from packages.core.services.file_context import bind_workspace_knowledge_documents

    entity_id = generate_ulid()
    if entity_state == "deleted":
        db_session.add(Entity(
            id=entity_id,
            name="Deleted binding entity",
            deleted_at=datetime.now(UTC),
        ))
    workspace = Workspace(entity_id=entity_id, name="Target")
    document = Document(
        entity_id=entity_id,
        name="brief.md",
        fs_path="User uploads/brief.md",
        visibility="private",
        metadata_={"origin": {"tool_name": "upload"}},
    )
    db_session.add_all([workspace, document])
    await db_session.flush()
    attachment = {"document_id": document.id}

    bound = await bind_workspace_knowledge_documents(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        user_id=generate_ulid(),
        attachments=[attachment],
    )

    assert bound == [attachment]
    assert document.visibility == "private"
    assert document.folder_id is None
    assert document.fs_path == "User uploads/brief.md"
    assert document.metadata_["origin"] == {"tool_name": "upload"}
    assert not (await db_session.scalars(
        select(DocumentGroupMember).where(DocumentGroupMember.document_id == document.id)
    )).all()
    assert not (await db_session.scalars(
        select(DocumentGroup).where(DocumentGroup.workspace_id == workspace.id)
    )).all()


@pytest.mark.asyncio
async def test_reconcile_entity_filesystem_syncs_real_paths_and_marks_missing_without_trashing(
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
    assert result.missing_documents == 1
    assert result.trashed_missing_documents == 0

    async with db_module.async_session() as db:
        stale = await db.get(Document, stale_id)
        assert stale is not None
        assert stale.is_trashed is False
        assert stale.vector_status == "ready"
        assert stale.metadata_["file_integrity"]["status"] == "missing"
        assert stale.metadata_["file_integrity"]["recoverable"] is True

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
