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

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "cloud"
    try:
        headers = await _auth(client, "docfsdown")
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
async def test_list_documents_hides_missing_filesystem_payload(db_session, tmp_path):
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

        assert total == 0
        assert all(item.id != doc_id for item in docs)

        stored = await db_session.get(Document, doc_id)
        assert stored is not None
        assert stored.is_trashed is True
        assert stored.metadata_["file_integrity"]["status"] == "missing"
        assert stored.vector_status == "failed"
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_missing_filesystem_payload_filter_marks_stale_doc(tmp_path, monkeypatch):
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

    assert visible == []
    assert doc.is_trashed is True
    assert doc.vector_status == VectorStatus.FAILED
    assert doc.metadata_["file_integrity"]["status"] == "missing"


@pytest.mark.asyncio
async def test_pending_filesystem_payload_is_hidden_when_file_is_missing(tmp_path, monkeypatch):
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

    assert visible == []
    assert doc.vector_status == VectorStatus.FAILED
    assert doc.is_trashed is True


@pytest.mark.asyncio
async def test_unavailable_entity_root_does_not_trash_all_documents(tmp_path, monkeypatch):
    from packages.core.models.document import VectorStatus
    from packages.core.services import document_access

    class FakeDb:
        async def flush(self):
            raise AssertionError("unavailable filesystem should not mutate rows")

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

    visible = await document_access._filter_readable_local_documents(FakeDb(), [doc])

    assert visible == [doc]
    assert doc.is_trashed is False


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
    assert doc.is_trashed is True
    assert doc.metadata_["file_integrity"]["status"] == "unavailable"


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
        "/api/v1/documents/upload", headers=headers, files={"file": ("contract.pdf", b"pdf", "application/pdf")}
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
