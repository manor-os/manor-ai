"""E2E tests: documents CRUD, upload, groups."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "docuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_upload_document(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("test.md", b"# Hello World\n\nThis is a test.", "text/markdown")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test.md"
    assert data["file_size"] > 0
    assert data["source"] == "upload"
    assert data["created_by"] == "docuser"


@pytest.mark.asyncio
async def test_upload_document_into_current_folder(client: AsyncClient):
    headers = await _auth(client, "docfolderupload")
    folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Receipts"},
    )
    assert folder_resp.status_code == 201, folder_resp.text
    folder = folder_resp.json()

    resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder['id']}",
        headers=headers,
        files={"file": ("receipt.md", b"# Receipt", "text/markdown")},
    )
    assert resp.status_code == 201
    doc = resp.json()
    assert doc["folder_id"] == folder["id"]

    folder_list = await client.get(
        f"/api/v1/documents?folder_id={folder['id']}",
        headers=headers,
    )
    assert folder_list.status_code == 200
    assert any(item["id"] == doc["id"] for item in folder_list.json()["items"])

    root_list = await client.get("/api/v1/documents?folder_id=root", headers=headers)
    assert root_list.status_code == 200
    assert all(item["id"] != doc["id"] for item in root_list.json()["items"])


@pytest.mark.asyncio
async def test_browse_documents_returns_root_direct_folders_and_files(client: AsyncClient):
    headers = await _auth(client, "docbrowseroot")

    root_doc_resp = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("root-daily.md", b"# Root daily", "text/markdown")},
    )
    assert root_doc_resp.status_code == 201, root_doc_resp.text
    root_doc = root_doc_resp.json()

    folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Daily"},
    )
    assert folder_resp.status_code == 201, folder_resp.text
    folder = folder_resp.json()

    nested_doc_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder['id']}",
        headers=headers,
        files={"file": ("nested-daily.md", b"# Nested daily", "text/markdown")},
    )
    assert nested_doc_resp.status_code == 201, nested_doc_resp.text

    browse = await client.get("/api/v1/documents/browse", headers=headers)

    assert browse.status_code == 200, browse.text
    payload = browse.json()
    assert [item["id"] for item in payload["folders"]] == [folder["id"]]
    assert [item["id"] for item in payload["documents"]] == [root_doc["id"]]
    assert payload["total_folders"] == 1
    assert payload["total"] == 1
    assert payload["total_documents"] == 1
    assert payload["total_files"] == 2


@pytest.mark.asyncio
async def test_indexing_status_poll_is_minimal_and_visibility_scoped(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import update

    from packages.core.models.document import Document

    owner_headers = await _auth(client, "docstatusowner")
    owner_upload = await client.post(
        "/api/v1/documents/upload",
        headers=owner_headers,
        files={"file": ("owner.md", b"# Owner", "text/markdown")},
    )
    assert owner_upload.status_code == 201, owner_upload.text
    owner_document_id = owner_upload.json()["id"]

    other_headers = await _auth(client, "docstatusother")
    other_upload = await client.post(
        "/api/v1/documents/upload",
        headers=other_headers,
        files={"file": ("other.md", b"# Other", "text/markdown")},
    )
    assert other_upload.status_code == 201, other_upload.text
    other_document_id = other_upload.json()["id"]

    await db_session.execute(
        update(Document)
        .where(Document.id == owner_document_id)
        .values(
            vector_status="processing",
            metadata_={
                "indexing": {
                    "run_id": "status-run",
                    "step": "embedding",
                    "current_chunk": 7,
                    "total_chunks": 20,
                }
            },
        )
    )
    await db_session.commit()

    response = await client.get(
        "/api/v1/documents/indexing-status",
        params=[("ids", owner_document_id), ("ids", other_document_id)],
        headers=owner_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": owner_document_id,
            "vector_status": "processing",
            "indexing_progress": {
                "run_id": "status-run",
                "step": "embedding",
                "current_chunk": 7,
                "total_chunks": 20,
            },
        }
    ]


@pytest.mark.asyncio
async def test_browse_documents_returns_folder_direct_folders_and_files(client: AsyncClient):
    headers = await _auth(client, "docbrowsefolder")
    parent_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Parent"},
    )
    assert parent_resp.status_code == 201, parent_resp.text
    parent = parent_resp.json()

    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Child", "parent_id": parent["id"]},
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()

    direct_doc_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={parent['id']}",
        headers=headers,
        files={"file": ("direct-daily.md", b"# Direct daily", "text/markdown")},
    )
    assert direct_doc_resp.status_code == 201, direct_doc_resp.text
    direct_doc = direct_doc_resp.json()

    nested_doc_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={child['id']}",
        headers=headers,
        files={"file": ("nested-daily.md", b"# Nested daily", "text/markdown")},
    )
    assert nested_doc_resp.status_code == 201, nested_doc_resp.text

    browse = await client.get(
        f"/api/v1/documents/browse?folder_id={parent['id']}",
        headers=headers,
    )

    assert browse.status_code == 200, browse.text
    payload = browse.json()
    assert [item["id"] for item in payload["folders"]] == [child["id"]]
    assert "folder_tree" not in payload
    assert [item["id"] for item in payload["documents"]] == [direct_doc["id"]]
    assert payload["total_folders"] == 1
    assert payload["total"] == 1
    assert payload["total_files"] == 2


@pytest.mark.asyncio
async def test_document_folder_tree_returns_visible_full_tree(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.services.document_service import create_document

    headers = await _auth(client, "docfoldertree")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]

    parent_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Parent"},
    )
    assert parent_resp.status_code == 201, parent_resp.text
    parent = parent_resp.json()

    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Child", "parent_id": parent["id"]},
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="nested-daily.md",
            fs_path=None,
            file_size=100,
            file_type="md",
            mime_type="text/markdown",
            source="manual",
            created_by="docfoldertree",
            folder_id=child["id"],
        )
        await db.commit()

    tree = await client.get("/api/v1/documents/folder-tree", headers=headers)

    assert tree.status_code == 200, tree.text
    payload = tree.json()
    assert [item["id"] for item in payload] == [parent["id"], child["id"]]
    assert payload[0]["document_count"] == 1
    assert payload[1]["document_count"] == 1


@pytest.mark.asyncio
async def test_browse_documents_search_is_global_and_includes_matching_folders(client: AsyncClient):
    headers = await _auth(client, "docbrowsesearch")
    daily_folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Daily reports"},
    )
    assert daily_folder_resp.status_code == 201, daily_folder_resp.text
    daily_folder = daily_folder_resp.json()

    archive_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Archive"},
    )
    assert archive_resp.status_code == 201, archive_resp.text
    archive = archive_resp.json()

    archive_doc_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={archive['id']}",
        headers=headers,
        files={"file": ("daily-product-progress.md", b"# Daily product progress", "text/markdown")},
    )
    assert archive_doc_resp.status_code == 201, archive_doc_resp.text
    archive_doc = archive_doc_resp.json()

    root_doc_resp = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("weekly.md", b"# Weekly", "text/markdown")},
    )
    assert root_doc_resp.status_code == 201, root_doc_resp.text

    browse = await client.get(
        f"/api/v1/documents/browse?folder_id={daily_folder['id']}&search=daily",
        headers=headers,
    )

    assert browse.status_code == 200, browse.text
    payload = browse.json()
    assert [item["id"] for item in payload["folders"]] == [daily_folder["id"]]
    assert [item["id"] for item in payload["documents"]] == [archive_doc["id"]]
    assert payload["total_folders"] == 1
    assert payload["total"] == 1
    assert payload["total_documents"] == 1


@pytest.mark.asyncio
async def test_browse_documents_returns_more_than_default_document_page(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.services.document_service import create_document

    headers = await _auth(client, "docbrowseall")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]

    async with db_module.async_session() as db:
        for index in range(105):
            await create_document(
                db,
                entity_id,
                name=f"daily-{index:03d}.md",
                fs_path=None,
                file_size=100,
                file_type="md",
                mime_type="text/markdown",
                source="manual",
                created_by="docbrowseall",
            )
        await db.commit()

    browse = await client.get("/api/v1/documents/browse?folder_id=root", headers=headers)

    assert browse.status_code == 200, browse.text
    payload = browse.json()
    assert len(payload["documents"]) == 105
    assert payload["total"] == 105
    assert payload["total_documents"] == 105


@pytest.mark.asyncio
async def test_browse_storage_used_matches_visible_active_knowledge(client: AsyncClient, monkeypatch):
    import packages.core.database as db_module
    from packages.core.constants import plans as plan_constants
    from packages.core.services import plan_enforcement
    from packages.core.services import plan_gate
    from packages.core.services.document_service import create_document

    visible_size = 10 * 1024 * 1024
    hidden_size = 150 * 1024 * 1024
    trashed_size = 40 * 1024 * 1024

    headers = await _auth(client, "docbrowsestoragevisible")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="visible.md",
            fs_path="visible.md",
            file_size=visible_size,
            file_type="md",
            mime_type="text/markdown",
            source="manual",
            created_by="docbrowsestoragevisible",
            skip_storage_check=True,
        )
        await create_document(
            db,
            entity_id,
            name="hidden.bin",
            fs_path="tmp/hidden.bin",
            file_size=hidden_size,
            file_type="bin",
            mime_type="application/octet-stream",
            source="manual",
            created_by="docbrowsestoragevisible",
            skip_storage_check=True,
        )
        trashed = await create_document(
            db,
            entity_id,
            name="trashed.mov",
            fs_path="trashed.mov",
            file_size=trashed_size,
            file_type="mov",
            mime_type="video/quicktime",
            source="manual",
            created_by="docbrowsestoragevisible",
            skip_storage_check=True,
        )
        trashed.is_trashed = True
        await db.commit()

    monkeypatch.setattr(plan_gate, "is_cloud", lambda: True)
    monkeypatch.setattr(plan_enforcement, "is_cloud", lambda: True)
    monkeypatch.setattr(plan_constants, "is_cloud", lambda: True)
    plan_gate.invalidate_gate_cache(entity_id)

    browse = await client.get("/api/v1/documents/browse?folder_id=root", headers=headers)

    assert browse.status_code == 200, browse.text
    payload = browse.json()
    assert [item["name"] for item in payload["documents"]] == ["visible.md"]
    assert payload["total_files"] == 1
    assert payload["total_size"] == visible_size
    assert payload["storage_used_mb"] == pytest.approx(10.0)
    assert payload["storage_limit_mb"] == 100


@pytest.mark.asyncio
async def test_move_document_to_folder_moves_filesystem_payload(client: AsyncClient, tmp_path):
    import packages.core.database as db_module
    from packages.core.config import get_settings
    from packages.core.services.document_service import create_document, get_document

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    try:
        headers = await _auth(client, "docmovefs")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        entity_id = me["entity_id"]
        entity_root = tmp_path / entity_id
        old_file = entity_root / "Old" / "brief.md"
        old_file.parent.mkdir(parents=True)
        old_file.write_text("# Brief\n", encoding="utf-8")

        async with db_module.async_session() as db:
            doc = await create_document(
                db,
                entity_id,
                name="brief.md",
                fs_path="Old/brief.md",
                file_size=old_file.stat().st_size,
                file_type="md",
                mime_type="text/markdown",
                source="upload",
            )
            await db.commit()
            doc_id = doc.id

        folder_resp = await client.post(
            "/api/v1/documents/folders",
            headers=headers,
            json={"name": "New"},
        )
        assert folder_resp.status_code == 201, folder_resp.text
        folder_id = folder_resp.json()["id"]

        moved_resp = await client.post(
            f"/api/v1/documents/{doc_id}/move",
            headers=headers,
            json={"folder_id": folder_id},
        )

        assert moved_resp.status_code == 200, moved_resp.text
        moved = moved_resp.json()
        assert moved["folder_id"] == folder_id
        assert moved["fs_path"] == "New/brief.md"
        assert not old_file.exists()
        assert (entity_root / "New" / "brief.md").is_file()

        async with db_module.async_session() as db:
            stored = await get_document(db, doc_id, entity_id)
            assert stored is not None
            assert stored.folder_id == folder_id
            assert stored.fs_path == "New/brief.md"
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_upload_rejects_when_cloud_filesystem_unavailable(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    headers = await _auth(client, "docfsdown")
    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "cloud"
    try:
        resp = await client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("lost.md", b"# Should not persist locally", "text/markdown")},
        )
        assert resp.status_code == 503
        assert "Document storage is temporarily unavailable" in resp.text

        listed = await client.get("/api/v1/documents", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total"] == 0
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_list_documents_keeps_missing_filesystem_payload_visible_without_stat(db_session, tmp_path):
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.services.document_access import list_visible_documents

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    try:
        entity_id = "ent_missingfs"
        (tmp_path / entity_id).mkdir(parents=True)

        doc = Document(
            entity_id=entity_id,
            name="missing.md",
            fs_path="docs/missing.md",
            file_type="md",
            mime_type="text/markdown",
            source="upload",
            vector_status="ready",
            created_by="docmissingfs",
        )
        db_session.add(doc)
        await db_session.commit()
        doc_id = doc.id

        docs, total = await list_visible_documents(
            db_session,
            entity_id,
            user_id="user_1",
            role="member",
        )

        assert total == 1
        assert [item.id for item in docs] == [doc_id]

        stored = await db_session.get(Document, doc_id)
        assert stored is not None
        assert stored.is_trashed is False
        # A listing is a DB-only projection. The periodic integrity repair
        # records missing files; browse/search must not stat or mutate every
        # visible document on the request path.
        assert "file_integrity" not in (stored.metadata_ or {})
        assert stored.vector_status == "ready"
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_missing_filesystem_payload_filter_keeps_stale_doc_visible_without_trashing_it(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        flushed = False

        async def flush(self):
            self.flushed = True

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/missing.md",
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.READY,
        is_trashed=False,
        trashed_at=None,
    )
    (tmp_path / "ent_1").mkdir()
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )

    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])

    assert visible == [doc]
    assert doc.is_trashed is False
    assert doc.vector_status == VectorStatus.READY
    assert doc.metadata_["file_integrity"]["status"] == "missing"


@pytest.mark.asyncio
async def test_pending_filesystem_payload_stays_visible_when_file_is_missing(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            return None

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/missing.md",
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.PENDING,
        is_trashed=False,
        trashed_at=None,
    )
    (tmp_path / "ent_1").mkdir()
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )

    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])

    assert visible == [doc]
    assert doc.vector_status == VectorStatus.PENDING
    assert doc.is_trashed is False
    assert doc.metadata_["file_integrity"]["status"] == "missing"
    assert doc.metadata_["file_integrity"]["recoverable"] is True


@pytest.mark.asyncio
async def test_unavailable_entity_root_does_not_trash_all_documents(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        flushed = False

        async def flush(self):
            self.flushed = True

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/report.md",
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.READY,
        is_trashed=False,
        trashed_at=None,
    )
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path / "missing-root")),
    )

    db = FakeDb()
    visible = await document_access._filter_readable_local_documents(db, [doc])

    assert visible == [doc]
    assert doc.is_trashed is False
    assert doc.metadata_["file_integrity"]["status"] == "unavailable"
    assert doc.metadata_["file_integrity"]["recoverable"] is True
    assert db.flushed is True


@pytest.mark.asyncio
async def test_failed_placeholder_without_payload_is_hidden_from_knowledge(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            return None

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path=None,
        file_url=None,
        metadata_={"external": {"source_url": "https://example.test/report"}},
        vector_status=VectorStatus.FAILED,
        is_trashed=False,
        trashed_at=None,
    )
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )

    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])

    assert visible == []
    assert doc.is_trashed is False
    assert doc.metadata_["file_integrity"]["status"] == "unavailable"
    assert doc.metadata_["file_integrity"]["recoverable"] is True


@pytest.mark.asyncio
async def test_aggregation_pass_skips_filesystem_stats(tmp_path, monkeypatch):
    """stat_files=False must not touch the filesystem and must not hide
    fs_path rows — folder counts and storage totals only need visibility,
    and fs_path rows are always visible regardless of stat outcome."""
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            raise AssertionError("aggregation pass should not mutate documents")

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/never-written.md",  # file does not exist
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.READY,
        is_trashed=False,
        trashed_at=None,
    )
    (tmp_path / "ent_1").mkdir()
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )
    stat_calls = {"count": 0}
    real_isfile = document_access.os.path.isfile

    def _counting_isfile(path):
        stat_calls["count"] += 1
        return real_isfile(path)

    monkeypatch.setattr(document_access.os.path, "isfile", _counting_isfile)

    visible = await document_access._filter_readable_local_documents(
        FakeDb(), [doc], stat_files=False,
    )

    assert visible == [doc]
    assert stat_calls["count"] == 0
    assert "file_integrity" not in doc.metadata_


@pytest.mark.asyncio
async def test_local_stat_results_are_cached_across_listings(tmp_path, monkeypatch):
    """A second listing within the TTL reuses the cached stat instead of
    re-hitting the (network) filesystem."""
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            return None

    (tmp_path / "ent_1" / "docs").mkdir(parents=True)
    (tmp_path / "ent_1" / "docs" / "report.md").write_text("hi")

    def _make_doc():
        return SimpleNamespace(
            id="doc_1",
            entity_id="ent_1",
            fs_path="docs/report.md",
            file_url=None,
            metadata_={},
            vector_status=VectorStatus.READY,
            is_trashed=False,
            trashed_at=None,
        )

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )
    document_access._clear_local_stat_cache()
    stat_calls = {"count": 0}
    real_isfile = document_access.os.path.isfile

    def _counting_isfile(path):
        stat_calls["count"] += 1
        return real_isfile(path)

    monkeypatch.setattr(document_access.os.path, "isfile", _counting_isfile)

    visible = await document_access._filter_readable_local_documents(FakeDb(), [_make_doc()])
    assert len(visible) == 1
    first_pass_calls = stat_calls["count"]
    assert first_pass_calls >= 1

    visible = await document_access._filter_readable_local_documents(FakeDb(), [_make_doc()])
    assert len(visible) == 1
    assert stat_calls["count"] == first_pass_calls  # served from cache

    # After the cache is dropped a stale entry cannot mask a deleted file.
    document_access._clear_local_stat_cache()
    (tmp_path / "ent_1" / "docs" / "report.md").unlink()
    doc = _make_doc()
    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])
    assert len(visible) == 1
    assert doc.metadata_["file_integrity"]["status"] == "missing"


@pytest.mark.asyncio
async def test_visible_document_listing_never_stats_the_filesystem(monkeypatch):
    """Knowledge browse/search must remain a DB-only read path.

    File existence is checked by open/download operations and the periodic
    integrity repair task, not once per visible row on every listing.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access, document_service

    document = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path="docs/report.md",
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.READY,
        is_trashed=False,
        trashed_at=None,
    )
    monkeypatch.setattr(
        document_service,
        "list_documents",
        AsyncMock(return_value=([document], 1)),
    )
    stat_local_documents = AsyncMock(
        side_effect=AssertionError("Knowledge listing must not stat the filesystem")
    )
    monkeypatch.setattr(
        document_access,
        "_stat_local_documents",
        stat_local_documents,
    )

    documents, total = await document_access.list_visible_documents(
        SimpleNamespace(),
        "ent_1",
        user_id=None,
    )

    assert documents == [document]
    assert total == 1
    stat_local_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_generating_placeholder_stays_visible_while_file_is_pending(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            raise AssertionError("generating placeholders should not be mutated")

    doc = SimpleNamespace(
        id="doc_1",
        entity_id="ent_1",
        fs_path=None,
        file_url=None,
        metadata_={},
        vector_status=VectorStatus.GENERATING,
        is_trashed=False,
        trashed_at=None,
    )
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ENABLED=True, MANOR_FS_ROOT=str(tmp_path)),
    )

    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])

    assert visible == [doc]
    assert doc.is_trashed is False


@pytest.mark.asyncio
async def test_list_documents(client: AsyncClient):
    headers = await _auth(client)
    # Upload 2 files
    await client.post("/api/v1/documents/upload", headers=headers, files={"file": ("a.txt", b"aaa", "text/plain")})
    await client.post("/api/v1/documents/upload", headers=headers, files={"file": ("b.txt", b"bbb", "text/plain")})

    resp = await client.get("/api/v1/documents", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_create_blank_diagram_document_opens_as_canvas(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "diagram_blank_user")
        created = await client.post(
            "/api/v1/documents/create-blank",
            headers=headers,
            json={"name": "System Canvas", "file_type": "diagram.json"},
        )

        assert created.status_code == 201
        doc = created.json()
        assert doc["name"] == "System Canvas.diagram.json"
        assert doc["file_type"] == "diagram.json"
        assert doc["mime_type"] == "application/json"

        content_resp = await client.get(
            f"/api/v1/documents/{doc['id']}/content",
            headers=headers,
        )
        assert content_resp.status_code == 200
        payload = json.loads(content_resp.json()["content"])
        assert payload["version"] == "editable_diagram_v1"
        assert payload["title"] == "System Canvas"
        assert payload["canvas"]["width"] == 2400
        assert payload["elements"] == []
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_create_blank_pptx_document_downloads_real_powerpoint(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "pptx_blank_user")
        created = await client.post(
            "/api/v1/documents/create-blank",
            headers=headers,
            json={"name": "Test Deck", "file_type": "pptx"},
        )

        assert created.status_code == 201
        doc = created.json()
        assert doc["name"] == "Test Deck.pptx"
        assert doc["file_type"] == "pptx"
        assert doc["mime_type"] == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        assert doc["file_size"] > 0

        download = await client.get(
            f"/api/v1/documents/{doc['id']}/download",
            headers=headers,
        )
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
            assert "ppt/presentation.xml" in zf.namelist()
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_downloading_legacy_ppt_never_rebuilds_or_overwrites_original(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "legacy_ppt_download_user")
        legacy_bytes = bytes.fromhex("d0cf11e0a1b11ae1") + b"legacy-powerpoint-binary-payload"
        upload = await client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("legacy-deck.ppt", legacy_bytes, "application/vnd.ms-powerpoint")},
        )
        assert upload.status_code == 201, upload.text

        download = await client.get(
            f"/api/v1/documents/{upload.json()['id']}/download",
            headers=headers,
        )
        assert download.status_code == 200, download.text
        assert download.content == legacy_bytes
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_slide_images_accept_pptx_metadata_without_name_extension(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True

    async def fake_render_slides(pptx_path: str, cache_dir: str):
        assert pptx_path.endswith("Personal Deck")
        cache_path = tmp_path / "rendered-slide.jpg"
        cache_path.write_bytes(b"jpeg")
        return [str(cache_path)]

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_slides",
        fake_render_slides,
    )

    try:
        headers = await _auth(client, "pptx_metadata_user")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        entity_id = me["entity_id"]
        entity_root = tmp_path / entity_id
        entity_root.mkdir(parents=True, exist_ok=True)
        (entity_root / "Personal Deck").write_bytes(b"PK\x03\x04pptx")

        doc = Document(
            entity_id=entity_id,
            name="Personal Deck",
            fs_path="Personal Deck",
            file_type="pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            file_size=8,
            source="agent",
            vector_status="ready",
            created_by="pptx_metadata_user",
        )
        db_session.add(doc)
        await db_session.commit()

        resp = await client.get(f"/api/v1/documents/{doc.id}/slides", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "slides": [{"index": 0, "url": f"/documents/{doc.id}/slides/0"}],
            "total": 1,
        }
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_document_thumbnail_uses_file_type_when_name_has_no_extension(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True

    async def fake_render_first_page(file_path: str, cache_dir: str, *, source_ext: str | None = None):
        assert file_path.endswith("Personal Deck")
        assert source_ext == ".pptx"
        cache_path = tmp_path / "thumbnail.jpg"
        cache_path.write_bytes(b"jpeg")
        return str(cache_path)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_first_page",
        fake_render_first_page,
    )

    try:
        headers = await _auth(client, "thumb_metadata_user")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        entity_id = me["entity_id"]
        entity_root = tmp_path / entity_id
        entity_root.mkdir(parents=True, exist_ok=True)
        (entity_root / "Personal Deck").write_bytes(b"PK\x03\x04pptx")

        doc = Document(
            entity_id=entity_id,
            name="Personal Deck",
            fs_path="Personal Deck",
            file_type="pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            file_size=8,
            source="agent",
            vector_status="ready",
            created_by="thumb_metadata_user",
        )
        db_session.add(doc)
        await db_session.commit()

        resp = await client.get(f"/api/v1/documents/{doc.id}/thumbnail", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.content == b"jpeg"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_document_content_uses_authorized_redis_hot_cache(client: AsyncClient, monkeypatch):
    from unittest.mock import AsyncMock

    from apps.api.routers import documents as documents_router

    headers = await _auth(client, "content_hot_cache_user")
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("cached.md", b"filesystem content", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text

    cache_get = AsyncMock(return_value="redis content")
    filesystem_get = AsyncMock(side_effect=AssertionError("Redis hit must bypass filesystem content read"))
    monkeypatch.setattr(documents_router, "get_cached_document_text", cache_get)
    monkeypatch.setattr(documents_router, "get_document_content", filesystem_get)

    response = await client.get(
        f"/api/v1/documents/{upload.json()['id']}/content",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"content": "redis content"}
    assert response.headers["x-knowledge-cache"] == "redis-hit"
    filesystem_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_thumbnail_uses_authorized_redis_hot_cache(client: AsyncClient, monkeypatch):
    from unittest.mock import AsyncMock

    from apps.api.routers import documents as documents_router
    from packages.core.services.knowledge_hot_cache import CachedKnowledgeBlob

    headers = await _auth(client, "thumbnail_hot_cache_user")
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={
            "file": (
                "cached.png",
                b"\x89PNG\r\n\x1a\noriginal image should not be read",
                "image/png",
            )
        },
    )
    assert upload.status_code == 201, upload.text

    cache_get = AsyncMock(
        return_value=CachedKnowledgeBlob(data=b"redis jpeg", media_type="image/jpeg")
    )
    monkeypatch.setattr(documents_router, "get_cached_document_blob", cache_get)

    response = await client.get(
        f"/api/v1/documents/{upload.json()['id']}/thumbnail",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.content == b"redis jpeg"
    assert response.headers["x-knowledge-cache"] == "redis-hit"


@pytest.mark.asyncio
async def test_generate_image_thumbnail_is_bounded_jpeg(tmp_path):
    from PIL import Image

    from apps.api.routers.documents import _generate_image_thumbnail

    source = tmp_path / "source.png"
    target = tmp_path / "thumbnail.jpg"
    Image.new("RGBA", (1600, 900), (30, 80, 120, 128)).save(source)

    await _generate_image_thumbnail(str(source), str(target))

    assert target.is_file()
    with Image.open(target) as thumbnail:
        assert thumbnail.format == "JPEG"
        assert thumbnail.width <= 640
        assert thumbnail.height <= 640


@pytest.mark.asyncio
async def test_search_documents(client: AsyncClient):
    headers = await _auth(client)
    await client.post(
        "/api/v1/documents/upload", headers=headers, files={"file": ("report.md", b"report content", "text/markdown")}
    )
    await client.post(
        "/api/v1/documents/upload", headers=headers, files={"file": ("invoice.pdf", b"pdf bytes", "application/pdf")}
    )

    resp = await client.get("/api/v1/documents?search=report", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["name"] == "report.md"


@pytest.mark.asyncio
async def test_delete_document(client: AsyncClient):
    headers = await _auth(client)
    upload = await client.post(
        "/api/v1/documents/upload", headers=headers, files={"file": ("todelete.txt", b"bye", "text/plain")}
    )
    doc_id = upload.json()["id"]

    resp = await client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_document_groups(client: AsyncClient):
    headers = await _auth(client)

    # Create a group
    group_resp = await client.post(
        "/api/v1/documents/groups",
        headers=headers,
        json={
            "name": "Contracts",
        },
    )
    assert group_resp.status_code == 201
    group_id = group_resp.json()["id"]

    # Upload a document
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("contract.pdf", b"%PDF-1.4\ncontract", "application/pdf")},
    )
    doc_id = upload.json()["id"]

    # Add to group
    resp = await client.post(f"/api/v1/documents/{doc_id}/groups/{group_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["added"]

    # Adding again should return false (already member)
    resp2 = await client.post(f"/api/v1/documents/{doc_id}/groups/{group_id}", headers=headers)
    assert not resp2.json()["added"]

    # List groups
    groups = await client.get("/api/v1/documents/groups", headers=headers)
    assert len(groups.json()) == 1
    assert groups.json()[0]["name"] == "Contracts"


@pytest.mark.asyncio
async def test_document_isolation(client: AsyncClient):
    headers_a = await _auth(client, "doc_a")
    headers_b = await _auth(client, "doc_b")

    upload = await client.post(
        "/api/v1/documents/upload", headers=headers_a, files={"file": ("secret.txt", b"secret", "text/plain")}
    )
    doc_id = upload.json()["id"]

    resp = await client.get(f"/api/v1/documents/{doc_id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_document_content_falls_back_to_legacy_metadata(monkeypatch):
    from packages.core.services import document_service

    legacy_doc = SimpleNamespace(
        entity_id="ent_legacy",
        fs_path=None,
        metadata_={"content_text": "# Legacy starter\n\nStored before fs_path projection."},
    )

    async def _fake_get_document(_db, _document_id, _entity_id):
        return legacy_doc

    monkeypatch.setattr(document_service, "get_document", _fake_get_document)

    content = await document_service.get_document_content(object(), "doc_legacy", "ent_legacy")

    assert content == "# Legacy starter\n\nStored before fs_path projection."


@pytest.mark.asyncio
async def test_save_legacy_metadata_document_allocates_fs_path(monkeypatch, tmp_path):
    from packages.core.services import document_service
    from packages.core.services import version_service

    legacy_doc = SimpleNamespace(
        id="01LEGACYDOC0000000000000",
        entity_id="ent_legacy",
        fs_path=None,
        name="Legacy starter.md",
        file_type="md",
        mime_type="text/markdown",
        metadata_={"content_text": "old"},
    )

    class _FakeDb:
        async def flush(self):
            return None

    async def _fake_get_document(_db, _document_id, _entity_id):
        return legacy_doc

    async def _fake_bump(_entity_id, _scope):
        return None

    async def _fake_create_version(*_args, **_kwargs):
        return None

    monkeypatch.setattr(document_service, "get_document", _fake_get_document)
    monkeypatch.setattr(document_service, "bump_tool_cache_version", _fake_bump)
    monkeypatch.setattr(version_service, "create_version", _fake_create_version)
    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(MANOR_FS_ROOT=str(tmp_path), MANOR_FS_ENABLED=True),
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_settings",
        lambda: SimpleNamespace(
            MANOR_FS_ROOT=str(tmp_path),
            MANOR_FS_ENABLED=True,
            DEPLOYMENT_MODE="oss",
        ),
    )

    ok = await document_service.save_document_content(
        _FakeDb(),
        "doc_legacy",
        "ent_legacy",
        "# Updated",
        created_by="tester",
    )

    assert ok is True
    assert legacy_doc.fs_path == "Legacy starter.md"
    assert (tmp_path / "ent_legacy" / "Legacy starter.md").read_text() == "# Updated"
