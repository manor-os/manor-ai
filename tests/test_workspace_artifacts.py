import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_artifacts import (
    artifact_folder_id_from_storage_path,
    ensure_workspace_artifact_directory,
    ensure_workspace_artifact_folder,
    resolve_workspace_folder_binding,
    workspace_artifact_display_path,
    workspace_artifact_storage_base,
)


def test_storage_path_exposes_one_canonical_folder_id() -> None:
    assert artifact_folder_id_from_storage_path(
        "/mnt/manor/entity/Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR/images/hero.png"
    ) == "01KQ9FOLDER8WJQC7KW18NYGR"
    assert artifact_folder_id_from_storage_path("Workspaces/Legacy Name/hero.png") is None


@pytest.mark.asyncio
async def test_workspace_artifact_directory_maps_requested_path_under_workspace(
    client: AsyncClient,
    db_session,
) -> None:
    headers = await _register(client)
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Product Video Studio"},
    )
    assert created.status_code == 201, created.text
    workspace = created.json()

    directory = await ensure_workspace_artifact_directory(
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
        directory_path=(
            "Product Video Studio/project-1/captures/screenshots"
        ),
    )

    assert directory.storage_path == (
        f"Workspaces/_by_id/{workspace['artifact_folder_id']}/"
        "Product Video Studio/project-1/captures/screenshots"
    )
    assert directory.display_path == (
        "Workspaces/Product Video Studio/"
        "Product Video Studio/project-1/captures/screenshots"
    )
    folder = (
        await db_session.execute(
            select(DocumentFolder).where(DocumentFolder.id == directory.folder_id)
        )
    ).scalar_one()
    assert folder.name == "screenshots"


async def _register(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "workspace_artifact_owner",
            "email": "workspace-artifact-owner@test.com",
            "password": "pass123",
            "entity_name": "Workspace Artifact Corp",
        },
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_deleted_workspace_folder_is_hidden_deletable_and_recreated(
    client: AsyncClient,
    db_session,
) -> None:
    from packages.core.services.document_service import create_document

    headers = await _register(client)
    active = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Active Workspace"},
    )).json()
    deleted = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Deleted Workspace"},
    )).json()

    await create_document(
        db_session,
        active["entity_id"],
        name="root.md",
        folder_id=None,
        file_size=25,
    )
    await create_document(
        db_session,
        active["entity_id"],
        name="shared-active.md",
        folder_id=active["artifact_folder_id"],
        file_size=100,
    )
    await create_document(
        db_session,
        active["entity_id"],
        name="shared-deleted.md",
        folder_id=deleted["artifact_folder_id"],
        file_size=900,
    )
    await db_session.commit()

    removed = await client.delete(
        f"/api/v1/workspaces/{deleted['id']}",
        headers=headers,
    )
    assert removed.status_code == 204

    tree = (await client.get(
        "/api/v1/documents/folder-tree",
        headers=headers,
    )).json()
    visible_folder_ids = {folder["id"] for folder in tree}
    assert active["artifact_folder_id"] in visible_folder_ids
    assert deleted["artifact_folder_id"] not in visible_folder_ids

    root_browse = (await client.get(
        "/api/v1/documents/browse",
        headers=headers,
    )).json()
    assert root_browse["total_documents"] == 1
    assert root_browse["total_files"] == 2
    assert root_browse["total_size"] == 125

    search_browse = (await client.get(
        "/api/v1/documents/browse?search=shared",
        headers=headers,
    )).json()
    assert [document["name"] for document in search_browse["documents"]] == [
        "shared-active.md"
    ]
    assert search_browse["total_files"] == 1
    assert search_browse["total_size"] == 100

    protected = await client.delete(
        f"/api/v1/documents/folders/{active['artifact_folder_id']}",
        headers=headers,
    )
    assert protected.status_code == 409

    cleaned = await client.delete(
        f"/api/v1/documents/folders/{deleted['artifact_folder_id']}",
        headers=headers,
    )
    assert cleaned.status_code == 204, cleaned.text

    db_session.expire_all()
    deleted_workspace = (await db_session.execute(
        select(Workspace).where(Workspace.id == deleted["id"])
    )).scalar_one()
    assert deleted_workspace.deleted_at is not None
    assert deleted_workspace.artifact_folder_id is None
    assert await db_session.get(DocumentFolder, deleted["artifact_folder_id"]) is None

    restored = await client.post(
        f"/api/v1/workspaces/{deleted['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    restored_folder_id = restored.json()["artifact_folder_id"]
    assert restored_folder_id
    assert restored_folder_id != deleted["artifact_folder_id"]
    assert await db_session.get(DocumentFolder, restored_folder_id) is not None


@pytest.mark.asyncio
async def test_workspace_api_upload_uses_folder_id_storage_and_rename_keeps_it(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from apps.api.routers import documents as documents_router
    from packages.core.services import entity_fs

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(entity_fs, "get_settings", lambda: documents_router.settings)
    headers = await _register(client)
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Customer Launch"},
    )
    assert created.status_code == 201, created.text
    workspace = created.json()
    folder_id = workspace["artifact_folder_id"]
    assert folder_id

    uploaded = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=headers,
        files={"file": ("brief.md", b"# Launch brief", "text/markdown")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["fs_path"].startswith(f"Workspaces/_by_id/{folder_id}/")
    assert uploaded.json()["display_path"] == "Workspaces/Customer Launch/brief.md"
    duplicate = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=headers,
        files={"file": ("brief.md", b"# Second brief", "text/markdown")},
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["fs_path"].startswith(f"Workspaces/_by_id/{folder_id}/")

    direct_folder_rename = await client.put(
        f"/api/v1/documents/folders/{folder_id}",
        headers=headers,
        json={"name": "Detached Name"},
    )
    assert direct_folder_rename.status_code == 200, direct_folder_rename.text
    assert direct_folder_rename.json()["id"] == folder_id
    assert direct_folder_rename.json()["name"] == "Detached Name"

    workspace_container_rename = await client.put(
        f"/api/v1/documents/folders/{direct_folder_rename.json()['parent_id']}",
        headers=headers,
        json={"name": "Detached Container"},
    )
    assert workspace_container_rename.status_code == 409

    workspace_after_folder_rename = await client.get(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
    )
    assert workspace_after_folder_rename.status_code == 200
    assert workspace_after_folder_rename.json()["name"] == "Detached Name"
    assert workspace_after_folder_rename.json()["artifact_folder_id"] == folder_id

    uploaded_after_folder_rename = await client.get(
        f"/api/v1/documents/{uploaded.json()['id']}",
        headers=headers,
    )
    assert uploaded_after_folder_rename.status_code == 200
    assert uploaded_after_folder_rename.json()["fs_path"].startswith(
        f"Workspaces/_by_id/{folder_id}/"
    )
    assert uploaded_after_folder_rename.json()["display_path"] == (
        "Workspaces/Detached Name/brief.md"
    )

    renamed = await client.put(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
        json={"name": "Customer Launch 2027"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["artifact_folder_id"] == folder_id

    folder = (await db_session.execute(
        select(DocumentFolder).where(DocumentFolder.id == folder_id)
    )).scalar_one()
    assert folder.name == "Customer Launch 2027"

    manual_path = (
        tmp_path
        / workspace["entity_id"]
        / "Workspaces"
        / "_by_id"
        / folder_id
        / "notes"
        / "manual.md"
    )
    manual_path.parent.mkdir(parents=True, exist_ok=True)
    manual_path.write_text("manually added", encoding="utf-8")
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    synced = await sync_file_to_knowledge(
        entity_id=workspace["entity_id"],
        abs_path=str(manual_path),
        entity_root=str(tmp_path / workspace["entity_id"]),
        source="filesystem_reconcile",
        force=True,
    )
    assert synced.synced is True
    document = (await db_session.execute(
        select(Document).where(Document.id == synced.document_id)
    )).scalar_one()
    assert document.metadata_["origin"]["workspace_id"] == workspace["id"]
    notes_folder = (await db_session.execute(
        select(DocumentFolder).where(DocumentFolder.id == document.folder_id)
    )).scalar_one()
    assert notes_folder.name == "notes"
    assert notes_folder.parent_id == folder_id


@pytest.mark.asyncio
async def test_manor_folder_actions_preserve_workspace_binding_and_id_storage(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import json

    import packages.core.database as db_module
    from packages.core.ai.tools.manor_tool import _dispatch_action
    from packages.core.config import get_settings
    from packages.core.services.document_service import create_document, get_document

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    headers = await _register(client)
    created = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Agent Files"},
    )
    assert created.status_code == 201, created.text
    workspace = created.json()
    folder_id = workspace["artifact_folder_id"]
    entity_root = tmp_path / workspace["entity_id"]
    source = entity_root / "incoming.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("incoming", encoding="utf-8")

    async with db_module.async_session() as db:
        document = await create_document(
            db,
            workspace["entity_id"],
            name="incoming.md",
            fs_path="incoming.md",
            file_size=source.stat().st_size,
            file_type="md",
            mime_type="text/markdown",
            source="upload",
        )
        await db.commit()
        document_id = document.id

    moved = json.loads(await _dispatch_action(
        "move_documents_to_folder",
        {"document_ids": [document_id], "folder_id": folder_id},
        workspace["entity_id"],
    ))
    assert moved["moved_count"] == 1
    expected_prefix = f"Workspaces/_by_id/{folder_id}/"
    assert moved["documents"][0]["fs_path"].startswith(expected_prefix)
    assert (entity_root / moved["documents"][0]["fs_path"]).is_file()

    async with db_module.async_session() as db:
        stored = await get_document(db, document_id, workspace["entity_id"])
        assert stored.metadata_["origin"]["workspace_id"] == workspace["id"]

    renamed = json.loads(await _dispatch_action(
        "rename_document_folder",
        {"folder_id": folder_id, "name": "Detached"},
        workspace["entity_id"],
    ))
    assert renamed["updated"] is True
    assert renamed["folder"]["id"] == folder_id
    assert renamed["folder"]["name"] == "Detached"

    workspace_after_folder_rename = await client.get(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
    )
    assert workspace_after_folder_rename.status_code == 200
    assert workspace_after_folder_rename.json()["name"] == "Detached"
    assert workspace_after_folder_rename.json()["artifact_folder_id"] == folder_id

    deleted = json.loads(await _dispatch_action(
        "delete_document_folder",
        {"folder_id": folder_id},
        workspace["entity_id"],
    ))
    assert "cannot be deleted" in deleted["error"]


@pytest.mark.asyncio
async def test_workspace_folder_id_survives_display_name_change(db_session) -> None:
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Launch Room",
        settings={},
    )
    db_session.add(workspace)
    await db_session.flush()

    folder = await ensure_workspace_artifact_folder(db_session, workspace)
    original_id = folder.id
    original_storage = workspace_artifact_storage_base(original_id)

    workspace.name = "Launch Room 2027"
    renamed = await ensure_workspace_artifact_folder(db_session, workspace)

    assert workspace.artifact_folder_id == original_id
    assert renamed.id == original_id
    assert renamed.name == "Launch Room 2027"
    assert workspace_artifact_storage_base(workspace.artifact_folder_id) == original_storage
    assert workspace_artifact_display_path(
        f"{original_storage}/images/hero.png",
        artifact_folder_id=original_id,
        display_folder_name=renamed.name,
    ) == "Workspaces/Launch Room 2027/images/hero.png"


@pytest.mark.asyncio
async def test_same_named_workspaces_get_distinct_folder_ids_and_display_names(db_session) -> None:
    entity_id = generate_ulid()
    first = Workspace(id=generate_ulid(), entity_id=entity_id, name="Campaign", settings={})
    second = Workspace(id=generate_ulid(), entity_id=entity_id, name="Campaign", settings={})
    db_session.add_all([first, second])
    await db_session.flush()

    first_folder = await ensure_workspace_artifact_folder(db_session, first)
    second_folder = await ensure_workspace_artifact_folder(db_session, second)

    assert first_folder.id != second_folder.id
    assert first_folder.name == "Campaign"
    assert second_folder.name.startswith("Campaign (")
    assert workspace_artifact_storage_base(first_folder.id) != workspace_artifact_storage_base(second_folder.id)


@pytest.mark.asyncio
async def test_legacy_workspace_reuses_unclaimed_visible_folder(db_session) -> None:
    entity_id = generate_ulid()
    root = DocumentFolder(id=generate_ulid(), entity_id=entity_id, name="Workspaces")
    legacy = DocumentFolder(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Existing Project",
        parent_id=root.id,
    )
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Existing Project",
        settings={},
    )
    legacy_document = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="legacy.md",
        fs_path="Workspaces/Existing Project/legacy.md",
        folder_id=legacy.id,
        metadata_={"origin": {"workspace_id": workspace.id}},
    )
    db_session.add_all([root, legacy, workspace, legacy_document])
    await db_session.flush()

    folder = await ensure_workspace_artifact_folder(db_session, workspace)

    assert folder.id == legacy.id
    assert workspace.artifact_folder_id == legacy.id


@pytest.mark.asyncio
async def test_same_named_user_folder_is_not_claimed_without_workspace_provenance(db_session) -> None:
    entity_id = generate_ulid()
    root = DocumentFolder(id=generate_ulid(), entity_id=entity_id, name="Workspaces")
    user_folder = DocumentFolder(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Research",
        parent_id=root.id,
    )
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Research", settings={})
    db_session.add_all([root, user_folder, workspace])
    await db_session.flush()

    folder = await ensure_workspace_artifact_folder(db_session, workspace)

    assert folder.id != user_folder.id
    assert folder.name.startswith("Research (")


@pytest.mark.asyncio
async def test_child_logical_folder_resolves_to_workspace_storage(db_session) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Design", settings={})
    db_session.add(workspace)
    await db_session.flush()
    root = await ensure_workspace_artifact_folder(db_session, workspace)
    child = DocumentFolder(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Images",
        parent_id=root.id,
    )
    db_session.add(child)
    await db_session.flush()

    binding = await resolve_workspace_folder_binding(
        db_session,
        entity_id=entity_id,
        folder_id=child.id,
    )

    assert binding is not None
    assert binding.workspace_id == workspace.id
    assert binding.artifact_folder_id == root.id
    assert binding.storage_dir == f"Workspaces/_by_id/{root.id}/Images"
    assert (await db_session.execute(
        select(DocumentFolder).where(DocumentFolder.id == root.id)
    )).scalar_one().name == "Design"
