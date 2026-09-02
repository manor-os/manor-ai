"""E2E tests: document versioning and trash/restore."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from packages.core.models.document import Document
from packages.core.models.comment import Comment
from packages.core.models.artifact_purge import WorkspaceArtifactPurgeJob
from packages.core.models.user import User
from packages.core.services.auth_service import create_access_token, hash_password
import packages.core.services.version_service as version_service
from packages.core.services.version_service import _dedupe_dest


def test_restore_destination_deduplication_never_reuses_an_existing_suffix(
    tmp_path,
    monkeypatch,
):
    original = tmp_path / "report.txt"
    original.write_text("active")
    first_restore = tmp_path / "report_123.txt"
    first_restore.write_text("already restored")
    monkeypatch.setattr("packages.core.services.version_service.time.time", lambda: 123)

    destination = _dedupe_dest(os.fspath(original))

    assert destination == os.fspath(tmp_path / "report_123_2.txt")


async def _auth(client: AsyncClient, username: str = "veruser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _upload(client: AsyncClient, headers: dict, name: str = "file.txt") -> dict:
    resp = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (name, b"hello world", "text/plain")},
    )
    assert resp.status_code == 201
    return resp.json()


async def _entity_member_headers(db_session, entity_id: str, username: str) -> dict:
    member = User(
        entity_id=entity_id,
        email=f"{username}@test.com",
        display_name=username,
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(member)
    await db_session.commit()
    token = create_access_token(member.id, entity_id, member.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_create_version(client: AsyncClient):
    headers = await _auth(client)
    doc = await _upload(client, headers)

    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "Initial snapshot"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["version_number"] == 1
    assert data["name"] == "file.txt"
    assert data["change_summary"] == "Initial snapshot"
    assert data["created_by"] == "veruser"


@pytest.mark.asyncio
async def test_list_versions(client: AsyncClient):
    headers = await _auth(client, "veruser2")
    doc = await _upload(client, headers)

    await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "v1"},
    )
    await client.post(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
        json={"change_summary": "v2"},
    )

    resp = await client.get(
        f"/api/v1/documents/{doc['id']}/versions",
        headers=headers,
    )
    assert resp.status_code == 200
    versions = resp.json()
    assert len(versions) == 2
    # Newest first
    assert versions[0]["version_number"] == 2
    assert versions[1]["version_number"] == 1


@pytest.mark.asyncio
async def test_trash_document(client: AsyncClient):
    headers = await _auth(client, "veruser3")
    doc = await _upload(client, headers)

    # Trash
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/trash",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["trashed"] is True

    # Should not appear in normal list
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0

    # Should appear in trash list
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert trash_resp.status_code == 200
    assert len(trash_resp.json()) == 1
    assert trash_resp.json()[0]["id"] == doc["id"]


@pytest.mark.asyncio
async def test_restore_document(client: AsyncClient):
    headers = await _auth(client, "veruser4")
    doc = await _upload(client, headers)

    # Trash then restore
    await client.post(f"/api/v1/documents/{doc['id']}/trash", headers=headers)
    resp = await client.post(f"/api/v1/documents/{doc['id']}/restore", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["restored"] is True

    # Should be back in normal list
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1
    assert list_resp.json()["items"][0]["id"] == doc["id"]

    # Should not be in trash
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert len(trash_resp.json()) == 0


@pytest.mark.asyncio
async def test_deleted_workspace_blocks_trash_restore_and_permanent_delete(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "deletedworkspacetrash")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restorable trash workspace"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    uploaded = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        params={"folder_id": workspace_data["artifact_folder_id"]},
        files={"file": ("restorable.txt", b"keep me", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    trashed = await client.post(
        f"/api/v1/documents/{document['id']}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text
    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_data['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    trash = await client.get("/api/v1/documents/trash", headers=headers)
    assert trash.status_code == 200, trash.text
    assert document["id"] not in {row["id"] for row in trash.json()}
    restore = await client.post(
        f"/api/v1/documents/{document['id']}/restore",
        headers=headers,
    )
    remove = await client.delete(
        f"/api/v1/documents/{document['id']}",
        headers=headers,
    )
    assert restore.status_code == 404, restore.text
    assert remove.status_code == 404, remove.text
    emptied = await client.post("/api/v1/documents/trash/empty", headers=headers)
    assert emptied.status_code == 204, emptied.text

    db_session.expire_all()
    preserved = await db_session.get(Document, document["id"])
    assert preserved is not None
    assert preserved.is_trashed is True

    restored_workspace = await client.post(
        f"/api/v1/workspaces/{workspace_data['id']}/restore",
        headers=headers,
    )
    assert restored_workspace.status_code == 200, restored_workspace.text
    restored_document = await client.post(
        f"/api/v1/documents/{document['id']}/restore",
        headers=headers,
    )
    assert restored_document.status_code == 200, restored_document.text


@pytest.mark.asyncio
async def test_member_cannot_list_restore_or_empty_another_users_trash(
    client: AsyncClient,
    db_session,
):
    owner_headers = await _auth(client, "vertrashowner")
    owner = await db_session.scalar(
        select(User).where(User.email == "vertrashowner@test.com")
    )
    assert owner is not None
    member_headers = await _entity_member_headers(
        db_session,
        owner.entity_id,
        "vertrashmember",
    )
    doc = await _upload(client, owner_headers, "owner-private.txt")
    trashed = await client.post(
        f"/api/v1/documents/{doc['id']}/trash",
        headers=owner_headers,
    )
    assert trashed.status_code == 200, trashed.text

    listed = await client.get("/api/v1/documents/trash", headers=member_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json() == []

    restored = await client.post(
        f"/api/v1/documents/{doc['id']}/restore",
        headers=member_headers,
    )
    assert restored.status_code == 403, restored.text

    emptied = await client.post(
        "/api/v1/documents/trash/empty",
        headers=member_headers,
    )
    assert emptied.status_code == 403, emptied.text

    owner_restored = await client.post(
        f"/api/v1/documents/{doc['id']}/restore",
        headers=owner_headers,
    )
    assert owner_restored.status_code == 200, owner_restored.text


@pytest.mark.asyncio
async def test_trash_list_reuses_one_batched_access_context(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import documents

    docs = [
        SimpleNamespace(id="one", owner_id=None, created_by=None),
        SimpleNamespace(id="two", owner_id=None, created_by=None),
    ]
    context = SimpleNamespace(
        is_admin=False,
        document_owned_by_deleted_workspace=lambda _document: False,
        preload_documents=AsyncMock(),
        effective_document_capabilities=AsyncMock(
            side_effect=[{documents.Capability.DELETE}, set()],
        ),
    )
    load_context = AsyncMock(return_value=context)
    monkeypatch.setattr(documents, "list_trash", AsyncMock(return_value=docs))
    monkeypatch.setattr(documents.DocumentAccessContext, "load", load_context)
    monkeypatch.setattr(
        documents,
        "effective_user_has_permission",
        AsyncMock(return_value=False),
    )
    responses: list[tuple[str, object]] = []

    async def doc_response(_db, doc, _user, access_ctx=None):
        responses.append((doc.id, access_ctx))
        return doc.id

    monkeypatch.setattr(documents, "_doc_resp_for_user", doc_response)
    user = SimpleNamespace(id="member", entity_id="entity", role="member")
    db = AsyncMock()

    result = await documents.list_trashed_documents(user=user, db=db)

    assert result == ["one"]
    load_context.assert_awaited_once_with(
        db,
        entity_id="entity",
        user_id="member",
        role="member",
    )
    context.preload_documents.assert_awaited_once_with(db, docs)
    assert responses == [("one", context)]


@pytest.mark.asyncio
async def test_restore_rejects_a_legacy_duplicate_active_path(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "verduplicate")
    user = await db_session.scalar(
        select(User).where(User.email == "verduplicate@test.com")
    )
    assert user is not None
    active = Document(
        entity_id=user.entity_id,
        name="current.html",
        fs_path="legacy-duplicate/current.html",
        created_by=user.id,
        owner_id=user.id,
    )
    duplicate = Document(
        entity_id=user.entity_id,
        name="older.html",
        fs_path="legacy-duplicate/current.html",
        created_by=user.id,
        owner_id=user.id,
        is_trashed=True,
    )
    db_session.add_all([active, duplicate])
    await db_session.commit()

    response = await client.post(
        f"/api/v1/documents/{duplicate.id}/restore",
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert "already uses this filesystem path" in response.json()["detail"]
    await db_session.refresh(duplicate)
    assert duplicate.is_trashed is True


@pytest.mark.asyncio
async def test_restore_rejects_a_nonrecoverable_deduplicated_projection(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "vernonrecoverable")
    user = await db_session.scalar(
        select(User).where(User.email == "vernonrecoverable@test.com")
    )
    assert user is not None
    duplicate = Document(
        entity_id=user.entity_id,
        name="duplicate.html",
        fs_path=None,
        created_by=user.id,
        owner_id=user.id,
        is_trashed=True,
        metadata_={
            "deduplicated_fs_path": "legacy/duplicate.html",
            "restore_blocked_reason": "duplicate_filesystem_projection",
        },
    )
    db_session.add(duplicate)
    await db_session.commit()

    response = await client.post(
        f"/api/v1/documents/{duplicate.id}/restore",
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert "no independently recoverable file" in response.json()["detail"]
    await db_session.refresh(duplicate)
    assert duplicate.is_trashed is True


@pytest.mark.asyncio
async def test_empty_trash(client: AsyncClient, db_session, monkeypatch, tmp_path):
    from apps.api.routers import documents as documents_router

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "veruser5")
    doc1 = await _upload(client, headers, "a.txt")
    doc2 = await _upload(client, headers, "b.txt")
    derived_cache_bases = []
    thumbnail_caches = []
    for doc in (doc1, doc2):
        for cache_root in (
            ".document-page-cache",
            ".slide-cache",
            ".slide-object-cache",
            ".doc-thumb-cache",
        ):
            cache_base = tmp_path / doc["entity_id"] / cache_root / doc["id"]
            version_cache = cache_base / "0123456789abcdef"
            version_cache.mkdir(parents=True)
            (version_cache / "preview.bin").write_bytes(b"preview")
            derived_cache_bases.append(cache_base)
        thumbnail_cache = (
            tmp_path
            / doc["entity_id"]
            / ".manor-cache"
            / "document-thumbnails"
            / f"{doc['id']}.jpg"
        )
        thumbnail_cache.parent.mkdir(parents=True, exist_ok=True)
        thumbnail_cache.write_bytes(b"thumbnail")
        thumbnail_caches.append(thumbnail_cache)
    for doc in (doc1, doc2):
        comment = await client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "resource_type": "document",
                "resource_id": doc["id"],
                "content": "Remove with trash",
            },
        )
        assert comment.status_code == 201, comment.text
    legacy_comment = (await db_session.execute(
        select(Comment).where(Comment.resource_id == doc2["id"])
    )).scalar_one()
    legacy_comment.resource_type = "Document"
    await db_session.commit()
    # Trash both
    await client.post(f"/api/v1/documents/{doc1['id']}/trash", headers=headers)
    await client.post(f"/api/v1/documents/{doc2['id']}/trash", headers=headers)
    trashed_source_files = []
    for doc in (doc1, doc2):
        trashed = await db_session.get(Document, doc["id"])
        await db_session.refresh(trashed)
        trashed_source_files.append(tmp_path / doc["entity_id"] / trashed.fs_path)
    assert all(source_file.is_file() for source_file in trashed_source_files)

    # Empty trash
    resp = await client.post("/api/v1/documents/trash/empty", headers=headers)
    assert resp.status_code == 204

    # Trash should be empty
    trash_resp = await client.get("/api/v1/documents/trash", headers=headers)
    assert len(trash_resp.json()) == 0

    # Normal list also empty (docs permanently deleted)
    list_resp = await client.get("/api/v1/documents", headers=headers)
    assert list_resp.json()["total"] == 0
    assert (await db_session.execute(
        select(Comment).where(Comment.resource_id.in_({doc1["id"], doc2["id"]}))
    )).scalars().all() == []
    assert all(not cache_base.exists() for cache_base in derived_cache_bases)
    assert all(not cache.exists() for cache in thumbnail_caches)
    assert all(not source_file.exists() for source_file in trashed_source_files)


@pytest.mark.asyncio
async def test_empty_trash_retains_source_cleanup_job_after_filesystem_failure(
    client: AsyncClient,
    db_session,
    monkeypatch,
    tmp_path,
):
    from apps.api.routers import documents as documents_router
    from packages.core.services import workspace_artifact_purge

    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(documents_router.settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "emptytrashsourcecleanupfailure")
    document = await _upload(client, headers, "empty-trash-cleanup.txt")
    await client.post(f"/api/v1/documents/{document['id']}/trash", headers=headers)
    trashed = await db_session.get(Document, document["id"])
    await db_session.refresh(trashed)
    source_file = tmp_path / document["entity_id"] / trashed.fs_path
    source_base = trashed.fs_path
    assert source_file.is_file()

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("trash source filesystem unavailable")

    monkeypatch.setattr(workspace_artifact_purge, "_remove_artifact_file", fail_cleanup)

    response = await client.post("/api/v1/documents/trash/empty", headers=headers)

    assert response.status_code == 204, response.text
    assert source_file.is_file()
    job = (await db_session.execute(
        select(WorkspaceArtifactPurgeJob).where(
            WorkspaceArtifactPurgeJob.entity_id == document["entity_id"],
            WorkspaceArtifactPurgeJob.storage_base == source_base,
            WorkspaceArtifactPurgeJob.target_kind == "file",
        )
    )).scalar_one()
    assert job.attempt_count == 1
    assert "trash source filesystem unavailable" in str(job.last_error)


@pytest.mark.asyncio
async def test_empty_trash_deletes_only_the_locked_snapshot(db_session, monkeypatch):
    entity_id = "empty-trash-snapshot"
    snapshot_doc = Document(
        entity_id=entity_id,
        name="already-trashed.txt",
        is_trashed=True,
    )
    late_doc = Document(
        entity_id=entity_id,
        name="trashed-after-snapshot.txt",
        is_trashed=False,
    )
    db_session.add_all([snapshot_doc, late_doc])
    await db_session.commit()
    snapshot_id = snapshot_doc.id
    late_id = late_doc.id

    async def trash_late_document(_db, _entity_id, _resource_type, resource_ids):
        assert resource_ids == [snapshot_id]
        late_doc.is_trashed = True
        await _db.flush()
        return 0

    monkeypatch.setattr(version_service, "delete_resource_comments", trash_late_document)

    deleted = await version_service.empty_trash(db_session, entity_id)

    assert deleted == 1
    assert await db_session.get(Document, snapshot_id) is None
    remaining = await db_session.get(Document, late_id)
    assert remaining is not None
    assert remaining.is_trashed is True


@pytest.mark.asyncio
async def test_permanently_delete_single_document_from_trash(client: AsyncClient):
    headers = await _auth(client, "veruser6")
    doc = await _upload(client, headers, "delete-from-trash.txt")

    trashed = await client.post(
        f"/api/v1/documents/{doc['id']}/trash",
        headers=headers,
    )
    assert trashed.status_code == 200, trashed.text

    deleted = await client.delete(
        f"/api/v1/documents/{doc['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    trash = await client.get("/api/v1/documents/trash", headers=headers)
    assert trash.status_code == 200, trash.text
    assert all(item["id"] != doc["id"] for item in trash.json())
