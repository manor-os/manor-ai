"""E2E tests: documents CRUD, upload, groups."""

import asyncio
import io
import json
import os
import shutil
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select, text

from packages.core.models.comment import Comment


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
async def test_folder_response_uses_batched_admin_context(monkeypatch):
    from apps.api.routers import documents

    async def unexpected_single_folder_check(*_args, **_kwargs):
        raise AssertionError("batched folder responses must not query admin per row")

    monkeypatch.setattr(documents, "_can_manage_folder", unexpected_single_folder_check)
    folder = SimpleNamespace(
        id="folder-1",
        entity_id="entity-1",
        name="Reports",
        parent_id=None,
        created_at=None,
        owner_id="another-user",
        visibility="private",
        classification=None,
        client_visible=False,
    )
    user = SimpleNamespace(id="admin-user", entity_id="entity-1")
    access_ctx = SimpleNamespace(
        is_admin=True,
        folder_capabilities=lambda _folder_id: set(),
    )

    response = await documents._folder_resp_for_user(
        None,
        folder,
        user,
        access_ctx=access_ctx,
    )

    assert "delete" in response.current_user_capabilities


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
async def test_list_documents_keeps_missing_filesystem_payload_visible_without_stat(db_session, tmp_path):
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.services.document_access import list_visible_documents

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.DEPLOYMENT_MODE = "oss"

    try:
        entity_id = generate_ulid()
        user_id = generate_ulid()
        (tmp_path / entity_id).mkdir(parents=True)

        entity = Entity(id=entity_id, name="Missing filesystem test")
        user = User(
            id=user_id,
            entity_id=entity_id,
            email=f"missingfs-{user_id.lower()}@test.com",
            password_hash="test",
            role="member",
            status="active",
        )
        membership = UserMembership(
            user_id=user_id,
            entity_id=entity_id,
            role="member",
            status="active",
            is_primary=True,
        )
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
        db_session.add_all([entity, user, membership, doc])
        await db_session.commit()
        doc_id = doc.id

        docs, total = await list_visible_documents(
            db_session,
            entity_id,
            user_id=user.id,
            role=user.role,
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

    class FakeContext:
        async def preload_documents(self, _db, _documents):
            return None

        async def can_read_document(self, _db, _document, **_kwargs):
            return True

    async def load_context(_cls, _db, **_kwargs):
        return FakeContext()

    monkeypatch.setattr(
        document_access.DocumentAccessContext,
        "load",
        classmethod(load_context),
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

        renamed = await client.put(
            f"/api/v1/documents/{doc['id']}",
            headers=headers,
            json={"name": "Renamed Canvas.json"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed Canvas.json"
        assert renamed.json()["file_type"] == "diagram.json"
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
async def test_legacy_office_file_exposes_editable_copy_without_overwriting_original(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
):
    from packages.core.config import get_settings
    from packages.core.services.office_editing import EditableOfficeFile

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    legacy_bytes = bytes.fromhex("d0cf11e0a1b11ae1") + b"legacy-powerpoint-binary-payload"

    async def fake_convert(
        source_path: str,
        source_name: str,
        *,
        source_format: str | None = None,
        source_mime: str | None = None,
    ):
        assert source_name == "legacy-deck.ppt"
        assert source_format == "ppt"
        assert source_mime == "application/vnd.ms-powerpoint"
        assert Path(source_path).read_bytes() == legacy_bytes
        editable_content = b"PK\x03\x04editable-pptx"
        return EditableOfficeFile(
            handle=io.BytesIO(editable_content),
            size=len(editable_content),
            filename="legacy-deck.pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

    monkeypatch.setattr(
        "packages.core.services.office_editing.convert_legacy_office_for_editing",
        fake_convert,
    )
    async def fake_convert_cached(
        source_path: str,
        source_name: str,
        _cache_dir: str,
        *,
        source_format: str | None = None,
        source_mime: str | None = None,
    ):
        return await fake_convert(
            source_path,
            source_name,
            source_format=source_format,
            source_mime=source_mime,
        )

    monkeypatch.setattr(
        "packages.core.services.office_editing.convert_legacy_office_for_editing_cached",
        fake_convert_cached,
    )
    async def fake_open_presentation_object(
        source_path: str,
        _cache_dir: str,
        *,
        slide_index: int,
        object_id: str,
    ):
        assert Path(source_path).read_bytes() == b"PK\x03\x04editable-pptx"
        assert slide_index == 0
        assert object_id == "42"
        rendered = tmp_path / "legacy-object.png"
        rendered.write_bytes(b"legacy-object-png")
        return rendered.open("rb")

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.open_presentation_object",
        fake_open_presentation_object,
    )
    try:
        headers = await _auth(client, "legacy_ppt_editable_copy_user")
        upload = await client.post(
            "/api/v1/documents/upload",
            headers=headers,
            files={"file": ("legacy-deck.ppt", legacy_bytes, "application/vnd.ms-powerpoint")},
        )
        assert upload.status_code == 201, upload.text

        editable = await client.get(
            f"/api/v1/documents/{upload.json()['id']}/editable-file",
            headers=headers,
        )
        assert editable.status_code == 200, editable.text
        assert editable.content == b"PK\x03\x04editable-pptx"
        assert "legacy-deck.pptx" in editable.headers["content-disposition"]

        object_image = await client.get(
            f"/api/v1/documents/{upload.json()['id']}/slides/0/objects/42",
            headers=headers,
        )
        assert object_image.status_code == 200, object_image.text
        assert object_image.content == b"legacy-object-png"

        async def unavailable_converter(*_args, **_kwargs):
            from packages.core.services.office_editing import OfficeConverterUnavailableError

            raise OfficeConverterUnavailableError("LibreOffice is unavailable")

        monkeypatch.setattr(
            "packages.core.services.office_editing.convert_legacy_office_for_editing",
            unavailable_converter,
        )
        unavailable = await client.get(
            f"/api/v1/documents/{upload.json()['id']}/editable-file",
            headers=headers,
        )
        assert unavailable.status_code == 503, unavailable.text

        original = await client.get(
            f"/api/v1/documents/{upload.json()['id']}/download",
            headers=headers,
        )
        assert original.content == legacy_bytes
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_editable_office_conversion_releases_database_and_streams_handle(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services.office_editing import EditableOfficeFile

    entity_id = "editable-stream-entity"
    document_id = "01M0EDITABLESTREAM0000000"
    source_path = tmp_path / entity_id / "legacy.ppt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"legacy")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="legacy.ppt",
        fs_path="legacy.ppt",
        file_type="ppt",
        mime_type="application/vnd.ms-powerpoint",
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")
    rollback_calls = 0
    editable_content = b"PK\x03\x04streamed-editable"
    converted_handle = io.BytesIO(editable_content)

    class FakeDb:
        async def rollback(self):
            nonlocal rollback_calls
            rollback_calls += 1

    async def visible_document(*_args, **_kwargs):
        return doc

    async def allow_edit(*_args, **_kwargs):
        return None

    async def convert_after_rollback(*_args, **_kwargs):
        assert rollback_calls >= 1
        return EditableOfficeFile(
            handle=converted_handle,
            size=len(editable_content),
            filename="legacy.pptx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(
        documents_router,
        "_require_document_capability",
        allow_edit,
    )
    monkeypatch.setattr(
        "packages.core.services.office_editing.convert_legacy_office_for_editing",
        convert_after_rollback,
    )

    response = await documents_router.get_editable_document_file(
        document_id,
        user=user,
        db=FakeDb(),
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == editable_content
    assert response.headers["content-length"] == str(len(body))
    assert rollback_calls >= 2
    assert converted_handle.closed is True


@pytest.mark.asyncio
async def test_editable_office_conversion_rejects_concurrent_source_replacement(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services.office_editing import EditableOfficeFile

    entity_id = "editable-stale-source"
    document_id = "01M0EDITABLESTALE00000000"
    source_path = tmp_path / entity_id / "legacy.ppt"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"legacy-before")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="legacy.ppt",
        fs_path="legacy.ppt",
        file_type="ppt",
        mime_type="application/vnd.ms-powerpoint",
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")
    converted_handle = io.BytesIO(b"PK\x03\x04stale-editable")

    class FakeDb:
        async def rollback(self):
            return None

    async def visible_document(*_args, **_kwargs):
        return doc

    async def allow_edit(*_args, **_kwargs):
        return None

    async def replace_during_conversion(*_args, **_kwargs):
        replacement = source_path.with_suffix(".replacement")
        replacement.write_bytes(b"legacy-after")
        os.replace(replacement, source_path)
        return EditableOfficeFile(
            handle=converted_handle,
            size=len(converted_handle.getvalue()),
            filename="legacy.pptx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
        )

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(documents_router, "_require_document_capability", allow_edit)
    monkeypatch.setattr(
        "packages.core.services.office_editing.convert_legacy_office_for_editing",
        replace_during_conversion,
    )

    with pytest.raises(HTTPException) as exc_info:
        await documents_router.get_editable_document_file(
            document_id,
            user=user,
            db=FakeDb(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "document_source_changed"
    assert converted_handle.closed is True


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
    render_calls = 0

    async def fake_render_slides(
        pptx_path: str,
        cache_dir: str,
        *,
        source_ext: str | None = None,
    ):
        nonlocal render_calls
        render_calls += 1
        assert pptx_path.endswith("Personal Deck")
        assert source_ext == "pptx"
        cache_path = Path(cache_dir) / "0123456789abcdef" / "slide-1.png"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"png")
        (cache_path.parent / ".complete").write_text("1", encoding="utf-8")
        return [str(cache_path)]

    async def fake_open_presentation_object(
        pptx_path: str,
        cache_dir: str,
        *,
        slide_index: int,
        object_id: str,
    ):
        assert pptx_path.endswith("Personal Deck")
        assert slide_index == 0
        assert object_id == "42"
        cache_path = tmp_path / "rendered-object.png"
        cache_path.write_bytes(b"object-png")
        return cache_path.open("rb")

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_slides",
        fake_render_slides,
    )
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.open_presentation_object",
        fake_open_presentation_object,
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
            "slides": [{
                "index": 0,
                "url": f"/documents/{doc.id}/slides/0?version=0123456789abcdef",
            }],
            "total": 1,
            "version": "0123456789abcdef",
        }

        image = await client.get(
            f"/api/v1/documents/{doc.id}/slides/0?version=0123456789abcdef",
            headers=headers,
        )
        assert image.status_code == 200, image.text
        assert image.content == b"png"
        assert image.headers["content-type"] == "image/png"
        assert "slide-1.png" in image.headers["content-disposition"]
        assert render_calls == 1

        object_image = await client.get(
            f"/api/v1/documents/{doc.id}/slides/0/objects/42",
            headers=headers,
        )
        assert object_image.status_code == 200, object_image.text
        assert object_image.content == b"object-png"
        assert object_image.headers["content-type"] == "image/png"

        async def oversized_presentation_object(*_args, **_kwargs):
            from packages.core.services.slide_renderer import PresentationObjectRenderLimitError

            raise PresentationObjectRenderLimitError(
                "Presentation object exceeds the render size limit",
            )

        monkeypatch.setattr(
            "packages.core.services.slide_renderer.open_presentation_object",
            oversized_presentation_object,
        )
        oversized = await client.get(
            f"/api/v1/documents/{doc.id}/slides/0/objects/42",
            headers=headers,
        )
        assert oversized.status_code == 413, oversized.text
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


def test_presentation_source_format_prefers_durable_metadata_after_rename():
    from apps.api.routers.documents import _is_pptx_document, _presentation_source_format

    renamed_legacy = SimpleNamespace(
        name="Quarterly Review.json",
        file_type="ppt",
        mime_type="application/vnd.ms-powerpoint",
    )

    assert _is_pptx_document(renamed_legacy) is True
    assert _presentation_source_format(renamed_legacy) == "ppt"


@pytest.mark.asyncio
async def test_slide_render_failure_does_not_expose_internal_error(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router

    entity_id = "slide-generic-error"
    document_id = "01M0SLIDEGENERICERROR000"
    source_path = tmp_path / entity_id / "Deck.pptx"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"PK\x03\x04pptx")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="Deck.pptx",
        fs_path="Deck.pptx",
        file_type="pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")

    class FakeDb:
        async def rollback(self):
            return None

    async def visible_document(*_args, **_kwargs):
        return doc

    async def failed_render(*_args, **_kwargs):
        raise RuntimeError("/private/office/profile leaked by renderer")

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_slides",
        failed_render,
    )

    with pytest.raises(HTTPException) as exc_info:
        await documents_router.get_slide_images(
            document_id,
            user=user,
            db=FakeDb(),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "Slide rendering failed"


@pytest.mark.asyncio
async def test_word_pages_preserve_pagination_for_docx_metadata_without_name_extension(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    original_bytes = b"PK\x03\x04docx-original"
    first_page_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (1632).to_bytes(4, "big")
        + (2112).to_bytes(4, "big")
    )
    second_page_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (2112).to_bytes(4, "big")
        + (1632).to_bytes(4, "big")
    )
    render_call_count = 0

    async def fake_render_document_pages(
        source_path: str,
        cache_dir: str,
        *,
        source_ext: str | None = None,
    ):
        nonlocal render_call_count
        render_call_count += 1
        assert source_path.endswith("Service Proposal")
        assert Path(source_path).read_bytes() == original_bytes
        assert source_ext == ".docx"
        rendered_dir = Path(cache_dir) / "0123456789abcdef"
        rendered_dir.mkdir(parents=True, exist_ok=True)
        first_page = rendered_dir / "page-1.png"
        second_page = rendered_dir / "page-2.png"
        first_page.write_bytes(first_page_bytes)
        second_page.write_bytes(second_page_bytes)
        (rendered_dir / ".complete").write_text("2", encoding="utf-8")
        return [str(first_page), str(second_page)]

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_document_pages",
        fake_render_document_pages,
    )

    try:
        headers = await _auth(client, "docx_page_metadata_user")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        entity_id = me["entity_id"]
        entity_root = tmp_path / entity_id
        entity_root.mkdir(parents=True, exist_ok=True)
        source_path = entity_root / "Service Proposal"
        source_path.write_bytes(original_bytes)

        doc = Document(
            entity_id=entity_id,
            name="Service Proposal",
            fs_path="Service Proposal",
            file_type="docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            file_size=len(original_bytes),
            source="upload",
            vector_status="ready",
            created_by="docx_page_metadata_user",
        )
        db_session.add(doc)
        await db_session.commit()

        response = await client.get(f"/api/v1/documents/{doc.id}/pages", headers=headers)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "pages": [
                {
                    "index": 0,
                    "url": f"/documents/{doc.id}/pages/0?version=0123456789abcdef",
                    "width": 1632,
                    "height": 2112,
                },
                {
                    "index": 1,
                    "url": f"/documents/{doc.id}/pages/1?version=0123456789abcdef",
                    "width": 2112,
                    "height": 1632,
                },
            ],
            "total": 2,
            "version": "0123456789abcdef",
        }

        # A managed save invalidates the current-version marker. Retained cache
        # files must not make removed content readable through an old URL.
        from packages.core.services.document_service import save_document_file

        updated_bytes = b"PK\x03\x04docx-updated"
        save_result = await save_document_file(
            db_session,
            doc.id,
            entity_id,
            updated_bytes,
            filename="Service Proposal.docx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            created_by="docx_page_metadata_user",
        )
        assert save_result is not None

        image = await client.get(
            f"/api/v1/documents/{doc.id}/pages/1?version=0123456789abcdef",
            headers=headers,
        )
        assert image.status_code == 404, image.text

        missing_page = await client.get(
            f"/api/v1/documents/{doc.id}/pages/2?version=0123456789abcdef",
            headers=headers,
        )
        assert missing_page.status_code == 404

        missing_version = await client.get(
            f"/api/v1/documents/{doc.id}/pages/1",
            headers=headers,
        )
        assert missing_version.status_code == 422
        assert render_call_count == 1
        assert source_path.read_bytes() == updated_bytes
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_word_page_manifest_releases_database_before_render(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router

    entity_id = "docx-short-transaction"
    document_id = "01M0DOCXSHORTTRANSACTION00"
    source_path = tmp_path / entity_id / "Service Proposal.docx"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"PK\x03\x04docx")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="Service Proposal.docx",
        fs_path="Service Proposal.docx",
        file_type="docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")
    transaction_released = False
    filesystem_lock_held = False

    class FakeDb:
        async def rollback(self):
            nonlocal transaction_released
            transaction_released = True

    async def visible_document(*_args, **_kwargs):
        return doc

    @asynccontextmanager
    async def filesystem_mutation(_entity_id):
        nonlocal filesystem_lock_held
        assert filesystem_lock_held is False
        filesystem_lock_held = True
        try:
            yield
        finally:
            filesystem_lock_held = False

    async def render_pages(_source, cache_dir, **_kwargs):
        assert transaction_released is True
        assert filesystem_lock_held is False
        page_dir = Path(cache_dir) / "0123456789abcdef"
        page_dir.mkdir(parents=True)
        page = page_dir / "page-1.png"
        page.write_bytes(b"page")
        (page_dir / ".complete").write_text("1", encoding="utf-8")
        return [str(page)]

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(
        documents_router,
        "_document_filesystem_mutation",
        filesystem_mutation,
    )
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_document_pages",
        render_pages,
    )

    result = await documents_router.get_document_page_images(
        document_id,
        user=user,
        db=FakeDb(),
    )

    assert transaction_released is True
    assert filesystem_lock_held is False
    assert result["version"] == "0123456789abcdef"


@pytest.mark.asyncio
async def test_word_page_manifest_rejects_source_changed_during_render(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router

    entity_id = "docx-stale-render"
    document_id = "01M0DOCXSTALERENDER00000"
    source_path = tmp_path / entity_id / "Proposal.docx"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"PK\x03\x04before")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="Proposal.docx",
        fs_path="Proposal.docx",
        file_type="docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")

    class FakeDb:
        async def rollback(self):
            return None

    async def visible_document(*_args, **_kwargs):
        return doc

    async def render_pages(_source, cache_dir, **_kwargs):
        replacement = source_path.with_suffix(".replacement")
        replacement.write_bytes(b"PK\x03\x04after")
        os.replace(replacement, source_path)
        page_dir = Path(cache_dir) / "0123456789abcdef"
        page_dir.mkdir(parents=True)
        page = page_dir / "page-1.png"
        page.write_bytes(b"stale-page")
        (page_dir / ".complete").write_text("1", encoding="utf-8")
        return [str(page)]

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_document_pages",
        render_pages,
    )

    with pytest.raises(HTTPException) as exc_info:
        await documents_router.get_document_page_images(
            document_id,
            user=user,
            db=FakeDb(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "document_source_changed"
    assert not (source_path.parent / ".document-page-cache" / document_id / ".current").exists()


@pytest.mark.asyncio
async def test_document_preview_removes_cache_recreated_after_permanent_deletion(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services import workspace_artifact_purge

    entity_id = "preview-delete-race"
    document_id = "01M0PREVIEWDELETERACE000"
    cache_file = (
        tmp_path
        / entity_id
        / ".slide-cache"
        / document_id
        / "0123456789abcdef"
        / "slide-1.png"
    )
    enqueued_bases = set()
    drained_bases = set()

    class MissingDocumentResult:
        def scalar_one_or_none(self):
            return None

    class FakeDb:
        async def rollback(self):
            return None

        async def execute(self, _statement):
            return MissingDocumentResult()

        async def commit(self):
            return None

    async def missing_document(*_args, **_kwargs):
        return None

    async def enqueue_cleanup(_db, cleanup_entity_id, storage_bases):
        assert cleanup_entity_id == entity_id
        enqueued_bases.update(storage_bases)
        return set(storage_bases)

    async def drain_cleanup(
        _db,
        *,
        entity_id: str,
        storage_bases,
        **_kwargs,
    ):
        assert entity_id == "preview-delete-race"
        drained_bases.update(storage_bases)
        for storage_base in storage_bases:
            target = tmp_path / entity_id / storage_base
            if target.is_dir():
                shutil.rmtree(target)
        return len(storage_bases), 0

    async def render_preview():
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"rendered-after-delete")
        return [str(cache_file)]

    monkeypatch.setattr(documents_router, "get_visible_document", missing_document)
    monkeypatch.setattr(
        workspace_artifact_purge,
        "enqueue_artifact_cleanup_jobs",
        enqueue_cleanup,
    )
    monkeypatch.setattr(
        workspace_artifact_purge,
        "drain_workspace_artifact_purge_jobs",
        drain_cleanup,
    )

    with pytest.raises(HTTPException) as exc_info:
        await documents_router._finish_document_preview(
            render_preview(),
            db=FakeDb(),
            doc_id=document_id,
            entity_id=entity_id,
            user_id="user-id",
            user_role="admin",
        )

    expected_bases = {
        f"{cache_root}/{document_id}"
        for cache_root in workspace_artifact_purge.DOCUMENT_DERIVED_TREE_CACHE_ROOTS
    }
    assert exc_info.value.status_code == 404
    assert enqueued_bases == expected_bases
    assert drained_bases == expected_bases
    assert cache_file.exists() is False


@pytest.mark.asyncio
async def test_word_page_manifest_revalidates_document_after_filesystem_lock(
    tmp_path,
    monkeypatch,
):
    from apps.api.routers import documents as documents_router

    entity_id = "docx-delete-race"
    document_id = "01M0DOCXDELETERACE000000"
    source_path = tmp_path / entity_id / "Service Proposal.docx"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"PK\x03\x04docx")
    doc = SimpleNamespace(
        id=document_id,
        entity_id=entity_id,
        name="Service Proposal.docx",
        fs_path="Service Proposal.docx",
        file_type="docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
    )
    user = SimpleNamespace(id="user-id", entity_id=entity_id, role="admin")
    visible_calls = 0
    rendered = False

    class FakeDb:
        async def rollback(self):
            return None

    async def visible_document(*_args, **_kwargs):
        nonlocal visible_calls
        visible_calls += 1
        return doc if visible_calls == 1 else None

    async def render_pages(*_args, **_kwargs):
        nonlocal rendered
        rendered = True
        return []

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(documents_router, "get_visible_document", visible_document)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.render_document_pages",
        render_pages,
    )

    with pytest.raises(HTTPException) as exc_info:
        await documents_router.get_document_page_images(
            document_id,
            user=user,
            db=FakeDb(),
        )

    assert exc_info.value.status_code == 404
    assert visible_calls == 2
    assert rendered is False


@pytest.mark.asyncio
async def test_document_thumbnail_uses_file_type_when_name_has_no_extension(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True

    async def fake_open_first_page(file_path: str, cache_dir: str, *, source_ext: str | None = None):
        assert file_path.endswith("Personal Deck")
        assert source_ext == ".pptx"
        cache_path = tmp_path / "thumbnail.jpg"
        cache_path.write_bytes(b"jpeg" * 300_000)
        return cache_path.open("rb"), str(cache_path)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.open_first_page",
        fake_open_first_page,
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

        class BulkReadMustNotBeUsed:
            def __init__(self, _path):
                pass

            def read_bytes(self):
                raise AssertionError("Office thumbnails must stream from a file handle")

        monkeypatch.setattr(documents_router, "Path", BulkReadMustNotBeUsed)
        resp = await client.get(f"/api/v1/documents/{doc.id}/thumbnail", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.content == b"jpeg" * 300_000
        assert "content-length" not in resp.headers
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
async def test_document_thumbnail_downloads_remote_image_when_file_missing(
    client: AsyncClient, db_session, tmp_path, monkeypatch
):
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    downloaded_paths: list[str] = []

    async def fake_download(file_url: str, target_path: str) -> None:
        assert file_url == "https://example.com/remote-image.jpg"
        downloaded_paths.append(target_path)
        from PIL import Image

        Image.new("RGB", (8, 8), "white").save(target_path, format="JPEG")

    try:
        monkeypatch.setattr(
            documents_router,
            "_download_remote_thumbnail_source",
            fake_download,
        )
        headers = await _auth(client, "remote_image_thumbnail_user")
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        doc = Document(
            entity_id=me["entity_id"],
            name="remote-image.jpg",
            fs_path=None,
            file_url="https://example.com/remote-image.jpg",
            file_type="jpg",
            mime_type="image/jpeg",
            file_size=8,
            source="chrome",
            vector_status="ready",
            created_by="remote_image_thumbnail_user",
        )
        db_session.add(doc)
        await db_session.commit()

        response = await client.get(f"/api/v1/documents/{doc.id}/thumbnail", headers=headers)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jpeg"
        assert len(downloaded_paths) == 1
        assert not os.path.exists(downloaded_paths[0])
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_generate_video_thumbnail_uses_jpeg_temporary_output(tmp_path, monkeypatch):
    from apps.api.routers import documents as documents_router

    output_paths: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command, **_kwargs):
        output_path = command[-1]
        output_paths.append(output_path)
        with open(output_path, "wb") as output:
            output.write(b"jpeg")
        return FakeProcess()

    monkeypatch.setattr(
        documents_router.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    target = tmp_path / "thumbnail.jpg"

    await documents_router._generate_video_thumbnail("source.mp4", str(target))

    assert target.read_bytes() == b"jpeg"
    assert output_paths == [str(tmp_path / "thumbnail.tmp.jpg")]


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
async def test_delete_document(client: AsyncClient, db_session, monkeypatch, tmp_path):
    from apps.api.routers import documents as documents_router

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client)
    upload = await client.post(
        "/api/v1/documents/upload", headers=headers, files={"file": ("todelete.txt", b"bye", "text/plain")}
    )
    doc_id = upload.json()["id"]
    entity_id = upload.json()["entity_id"]
    source_file = tmp_path / entity_id / upload.json()["fs_path"]
    assert source_file.is_file()
    derived_cache_bases = [
        tmp_path / entity_id / cache_root / doc_id
        for cache_root in (
            ".document-page-cache",
            ".slide-cache",
            ".slide-object-cache",
            ".doc-thumb-cache",
        )
    ]
    for cache_base in derived_cache_bases:
        version_cache = cache_base / "0123456789abcdef"
        version_cache.mkdir(parents=True)
        (version_cache / "preview.bin").write_bytes(b"preview")
    thumbnail_cache = (
        tmp_path
        / entity_id
        / ".manor-cache"
        / "document-thumbnails"
        / f"{doc_id}.jpg"
    )
    thumbnail_cache.parent.mkdir(parents=True)
    thumbnail_cache.write_bytes(b"thumbnail")

    comment = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            "resource_type": "document",
            "resource_id": doc_id,
            "content": "Delete this with the document",
        },
    )
    assert comment.status_code == 201, comment.text

    resp = await client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp2.status_code == 404
    assert (await db_session.execute(
        select(Comment).where(Comment.resource_id == doc_id)
    )).scalar_one_or_none() is None
    assert all(not cache_base.exists() for cache_base in derived_cache_bases)
    assert not thumbnail_cache.exists()
    assert not source_file.exists()


@pytest.mark.asyncio
async def test_delete_document_retains_page_cleanup_job_after_filesystem_failure(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    from apps.api.routers import documents as documents_router
    from packages.core.models.artifact_purge import WorkspaceArtifactPurgeJob
    from packages.core.services import workspace_artifact_purge

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "documentcachecleanupfailure")
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("cleanup-failure.txt", b"bye", "text/plain")},
    )
    document = upload.json()
    page_cache = (
        tmp_path
        / document["entity_id"]
        / ".document-page-cache"
        / document["id"]
        / "0123456789abcdef"
    )
    page_cache.mkdir(parents=True)
    (page_cache / "page-1.png").write_bytes(b"page")

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(workspace_artifact_purge, "_remove_artifact_tree", fail_cleanup)

    response = await client.delete(
        f"/api/v1/documents/{document['id']}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert page_cache.exists()
    job = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == document["entity_id"],
            WorkspaceArtifactPurgeJob.storage_base
            == f".document-page-cache/{document['id']}",
        )
    )).scalar_one()
    assert job.attempt_count == 1
    assert "filesystem unavailable" in str(job.last_error)


@pytest.mark.asyncio
async def test_delete_document_retains_source_cleanup_job_after_filesystem_failure(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    from apps.api.routers import documents as documents_router
    from packages.core.models.artifact_purge import WorkspaceArtifactPurgeJob
    from packages.core.services import workspace_artifact_purge

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "documentsourcecleanupfailure")
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("source-cleanup-failure.txt", b"bye", "text/plain")},
    )
    document = upload.json()
    source_file = tmp_path / document["entity_id"] / document["fs_path"]
    assert source_file.is_file()

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("source filesystem unavailable")

    monkeypatch.setattr(workspace_artifact_purge, "_remove_artifact_file", fail_cleanup)

    response = await client.delete(
        f"/api/v1/documents/{document['id']}",
        headers=headers,
    )

    assert response.status_code == 204, response.text
    assert source_file.is_file()
    job = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == document["entity_id"],
            WorkspaceArtifactPurgeJob.storage_base == document["fs_path"],
            WorkspaceArtifactPurgeJob.target_kind == "file",
        )
    )).scalar_one()
    assert job.attempt_count == 1
    assert "source filesystem unavailable" in str(job.last_error)


@pytest.mark.asyncio
async def test_delete_document_serializes_against_comment_create(client: AsyncClient, db_session, monkeypatch):
    from apps.api.routers import comments as comments_router
    from packages.core import database as db_module
    from packages.core.services import document_service

    headers = await _auth(client, "doccommentdelete")
    upload = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("comment-race.txt", b"race", "text/plain")},
    )
    doc_id = upload.json()["id"]
    loop = asyncio.get_running_loop()
    cleanup_reached = asyncio.Event()
    release_delete = asyncio.Event()
    delete_query_pid: asyncio.Future[int] = loop.create_future()
    comment_query_pid: asyncio.Future[int] = loop.create_future()
    original_cleanup = document_service.delete_resource_comments
    original_get_document_for_update = comments_router.get_document_for_update

    async def paused_cleanup(db, *args, **kwargs):
        count = await original_cleanup(db, *args, **kwargs)
        pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        delete_query_pid.set_result(int(pid))
        cleanup_reached.set()
        await release_delete.wait()
        return count

    async def tracked_get_document_for_update(db, *args, **kwargs):
        pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        if not comment_query_pid.done():
            comment_query_pid.set_result(int(pid))
        return await original_get_document_for_update(db, *args, **kwargs)

    monkeypatch.setattr(document_service, "delete_resource_comments", paused_cleanup)
    monkeypatch.setattr(
        comments_router,
        "get_document_for_update",
        tracked_get_document_for_update,
    )
    delete_task = asyncio.create_task(
        client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    )
    await asyncio.wait_for(cleanup_reached.wait(), timeout=2)
    comment_task = asyncio.create_task(
        client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "resource_type": "document",
                "resource_id": doc_id,
                "content": "Must not become orphaned",
            },
        )
    )
    try:
        delete_pid = await asyncio.wait_for(delete_query_pid, timeout=2)
        comment_pid = await asyncio.wait_for(comment_query_pid, timeout=2)
        async with db_module.async_session() as observer_db:
            blocking_pids: list[int] = []
            deadline = loop.time() + 2
            while loop.time() < deadline:
                blocking_pids = list(
                    await observer_db.scalar(
                        text("SELECT pg_blocking_pids(:pid)"),
                        {"pid": comment_pid},
                    )
                    or []
                )
                if delete_pid in blocking_pids:
                    break
                await asyncio.sleep(0.01)
            assert delete_pid in blocking_pids
    finally:
        release_delete.set()
        delete_response, comment_response = await asyncio.gather(delete_task, comment_task)
    assert delete_response.status_code == 204
    assert comment_response.status_code == 404
    db_session.expire_all()
    assert (await db_session.execute(
        select(Comment).where(Comment.resource_id == doc_id)
    )).scalar_one_or_none() is None


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

    async def _fake_get_document(_db, _document_id, _entity_id, **_kwargs):
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

        async def commit(self):
            return None

    async def _fake_get_document(_db, _document_id, _entity_id, **_kwargs):
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
