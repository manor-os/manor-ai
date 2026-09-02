from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "folderuser") -> tuple[dict[str, str], str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Folder Corp",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["entity_id"]


@pytest.mark.asyncio
async def test_concurrent_fs_path_upserts_resolve_to_one_document(client: AsyncClient):
    import asyncio

    from sqlalchemy import func, select

    import packages.core.database as db_module
    from packages.core.models.document import Document
    from packages.core.services.document_service import upsert_document_by_fs_path

    _, entity_id = await _auth(client, "folder_concurrent_path")
    start = asyncio.Event()

    async def writer(name: str) -> str:
        async with db_module.async_session() as db:
            await start.wait()
            document = await upsert_document_by_fs_path(
                db,
                entity_id,
                fs_path="shared/report.md",
                name=name,
                file_type="md",
                source="filesystem",
                skip_storage_check=True,
            )
            await db.commit()
            return document.id

    writes = [asyncio.create_task(writer("first.md")), asyncio.create_task(writer("second.md"))]
    start.set()
    document_ids = await asyncio.gather(*writes)

    assert document_ids[0] == document_ids[1]
    async with db_module.async_session() as db:
        active_count = await db.scalar(
            select(func.count(Document.id)).where(
                Document.entity_id == entity_id,
                Document.fs_path == "shared/report.md",
                Document.is_trashed.is_(False),
            )
        )
    assert active_count == 1


@pytest.mark.asyncio
async def test_fs_path_upsert_does_not_transfer_existing_document_ownership(monkeypatch):
    from types import SimpleNamespace

    from packages.core.services import document_service
    from packages.core.services.document_service import upsert_document_by_fs_path

    existing = SimpleNamespace(
        id="document-owned-by-alice",
        entity_id="entity-owner-preservation",
        fs_path="shared/report.md",
        name="report.md",
        file_size=4,
        file_type="md",
        mime_type="text/markdown",
        source="upload",
        created_by="alice",
        owner_id="alice",
        folder_id=None,
        visibility="private",
        classification="internal",
        client_visible=True,
        is_trashed=False,
        trashed_at=None,
        trashed_by=None,
    )

    class ExistingResult:
        def scalar_one_or_none(self):
            return existing

    class ExistingDocumentSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, _statement):
            return ExistingResult()

    async def no_op_cache_bump(*_args, **_kwargs):
        return None

    monkeypatch.setattr(document_service, "bump_tool_cache_version", no_op_cache_bump)

    updated = await upsert_document_by_fs_path(
        ExistingDocumentSession(),
        existing.entity_id,
        fs_path=existing.fs_path,
        name=existing.name,
        file_size=12,
        file_type="md",
        mime_type="text/markdown",
        source="filesystem",
        created_by="bob",
        owner_id="bob",
    )

    assert updated.owner_id == "alice"
    assert updated.file_size == 12


@pytest.mark.asyncio
async def test_folder_list_counts_documents_recursively(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.services.document_service import create_document

    headers, entity_id = await _auth(client, "folder_recursive")

    root_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Projects"},
    )
    assert root_resp.status_code == 201
    root_id = root_resp.json()["id"]

    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Reports", "parent_id": root_id},
    )
    assert child_resp.status_code == 201
    child_id = child_resp.json()["id"]

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="root-note.md",
            file_type="md",
            source="upload",
            folder_id=root_id,
        )
        await create_document(
            db,
            entity_id,
            name="child-note.md",
            file_type="md",
            source="upload",
            folder_id=child_id,
        )
        await db.commit()

    listed = await client.get("/api/v1/documents/folders", headers=headers)
    assert listed.status_code == 200
    counts = {folder["id"]: folder["document_count"] for folder in listed.json()}

    assert counts[root_id] == 2
    assert counts[child_id] == 1


@pytest.mark.asyncio
async def test_storage_usage_includes_nested_subfolders(client: AsyncClient):
    """The list response's storage totals must recurse into subfolders, not
    just count the files sitting directly at the current level."""
    import packages.core.database as db_module
    from packages.core.services.document_service import create_document

    headers, entity_id = await _auth(client, "folder_storage")

    root_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Library"},
    )
    root_id = root_resp.json()["id"]
    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Sub", "parent_id": root_id},
    )
    child_id = child_resp.json()["id"]

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="top.md",
            file_type="md",
            source="upload",
            folder_id=root_id,
            file_size=100,
        )
        await create_document(
            db,
            entity_id,
            name="nested.md",
            file_type="md",
            source="upload",
            folder_id=child_id,
            file_size=250,
        )
        await db.commit()

    # Viewing the parent folder: storage must include the nested file (100+250),
    # even though only the one direct child is listed on the page.
    resp = await client.get(
        f"/api/v1/documents?folder_id={root_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1  # direct children only, as before
    assert body["total"] == 1
    assert body["total_files"] == 2  # recursive
    assert body["total_size"] == 350  # recursive: 100 + 250

    browse = (
        await client.get(
            f"/api/v1/documents/browse?folder_id={root_id}",
            headers=headers,
        )
    ).json()
    assert browse["total_files"] == 2
    assert browse["total_size"] == 350
    assert browse["direct_total_files"] == 1
    assert browse["direct_total_size"] == 100

    # The leaf folder only sees its own file.
    leaf = (
        await client.get(
            f"/api/v1/documents?folder_id={child_id}",
            headers=headers,
        )
    ).json()
    assert leaf["total_files"] == 1
    assert leaf["total_size"] == 250


@pytest.mark.asyncio
async def test_folder_search_recurses_into_descendant_folders(client: AsyncClient):
    """Searching from a folder should behave like filesystem search: normal
    browsing lists direct children, while search includes descendant matches."""
    import packages.core.database as db_module
    from packages.core.services.document_service import create_document

    headers, entity_id = await _auth(client, "folder_search_recursive")

    root_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Projects"},
    )
    root_id = root_resp.json()["id"]
    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Reports", "parent_id": root_id},
    )
    child_id = child_resp.json()["id"]

    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="roadmap.md",
            file_type="md",
            source="upload",
            folder_id=root_id,
            file_size=100,
        )
        nested = await create_document(
            db,
            entity_id,
            name="nested-market-report.md",
            file_type="md",
            source="upload",
            folder_id=child_id,
            file_size=250,
        )
        await db.commit()
        nested_id = nested.id

    parent = (
        await client.get(
            f"/api/v1/documents?folder_id={root_id}",
            headers=headers,
        )
    ).json()
    assert [item["name"] for item in parent["items"]] == ["roadmap.md"]

    searched_parent = (
        await client.get(
            f"/api/v1/documents?folder_id={root_id}&search=nested-market",
            headers=headers,
        )
    ).json()
    assert searched_parent["total"] == 1
    assert searched_parent["items"][0]["id"] == nested_id

    searched_root = (
        await client.get(
            "/api/v1/documents?folder_id=root&search=nested-market",
            headers=headers,
        )
    ).json()
    assert searched_root["total"] == 1
    assert searched_root["items"][0]["id"] == nested_id


@pytest.mark.asyncio
async def test_create_document_blocked_when_over_storage_limit(client: AsyncClient, monkeypatch):
    """create_document is the single chokepoint for the plan storage limit: it
    refuses new docs when over quota, but the reconcile/bookkeeping bypass still
    works."""
    import packages.core.database as db_module
    from packages.core.services import plan_gate
    from packages.core.services.document_service import StorageLimitExceeded, create_document

    headers, entity_id = await _auth(client, "storage_gate")

    async def _denied(_db, _entity_id, _resource):
        return plan_gate.GateResult(
            allowed=False,
            message="full",
            limit=100,
            current=150,
            plan="Free",
        )

    monkeypatch.setattr(plan_gate, "check", _denied)

    async with db_module.async_session() as db:
        with pytest.raises(StorageLimitExceeded):
            await create_document(
                db,
                entity_id,
                name="blocked.md",
                file_type="md",
                source="upload",
            )
        # Bookkeeping (e.g. filesystem reconcile) must not be blocked.
        doc = await create_document(
            db,
            entity_id,
            name="reconciled.md",
            file_type="md",
            source="filesystem_reconcile",
            skip_storage_check=True,
        )
        assert doc.id
        await db.commit()


@pytest.mark.asyncio
async def test_delete_folder_removes_nested_contents(client: AsyncClient):
    import packages.core.database as db_module
    from sqlalchemy import select

    from packages.core.models.comment import Comment
    from packages.core.services.document_service import create_document, get_document

    headers, entity_id = await _auth(client, "folder_delete_tree")

    root_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Delete Me"},
    )
    assert root_resp.status_code == 201
    root_id = root_resp.json()["id"]

    child_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Nested", "parent_id": root_id},
    )
    assert child_resp.status_code == 201
    child_id = child_resp.json()["id"]

    sibling_resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Keep Me"},
    )
    assert sibling_resp.status_code == 201
    sibling_id = sibling_resp.json()["id"]

    async with db_module.async_session() as db:
        root_doc = await create_document(
            db,
            entity_id,
            name="root-note.md",
            file_type="md",
            source="upload",
            folder_id=root_id,
        )
        child_doc = await create_document(
            db,
            entity_id,
            name="child-note.md",
            file_type="md",
            source="upload",
            folder_id=child_id,
        )
        sibling_doc = await create_document(
            db,
            entity_id,
            name="sibling-note.md",
            file_type="md",
            source="upload",
            folder_id=sibling_id,
        )
        await db.commit()
        root_doc_id = root_doc.id
        child_doc_id = child_doc.id
        sibling_doc_id = sibling_doc.id

    for document_id, content in (
        (root_doc_id, "Delete with root folder"),
        (child_doc_id, "Delete with child folder"),
        (sibling_doc_id, "Keep with sibling folder"),
    ):
        comment = await client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "resource_type": "document",
                "resource_id": document_id,
                "content": content,
            },
        )
        assert comment.status_code == 201, comment.text

    deleted = await client.delete(f"/api/v1/documents/folders/{root_id}", headers=headers)
    assert deleted.status_code == 204

    listed = await client.get("/api/v1/documents/folders", headers=headers)
    assert listed.status_code == 200
    folder_ids = {folder["id"] for folder in listed.json()}
    assert root_id not in folder_ids
    assert child_id not in folder_ids
    assert sibling_id in folder_ids

    async with db_module.async_session() as db:
        assert await get_document(db, root_doc_id, entity_id) is None
        assert await get_document(db, child_doc_id, entity_id) is None
        kept_doc = await get_document(db, sibling_doc_id, entity_id)
        assert kept_doc is not None
        assert kept_doc.folder_id == sibling_id
        remaining_comment_resource_ids = set((await db.execute(
            select(Comment.resource_id).where(
                Comment.entity_id == entity_id,
                Comment.resource_id.in_([root_doc_id, child_doc_id, sibling_doc_id]),
            )
        )).scalars().all())
        assert remaining_comment_resource_ids == {sibling_doc_id}


@pytest.mark.asyncio
async def test_move_path_updates_folder_id_for_file_moves(client: AsyncClient):
    import packages.core.database as db_module
    from packages.core.models.document import Document
    from packages.core.services.document_service import create_document
    from packages.core.services.knowledge_sync import find_folder_path, move_path

    _, entity_id = await _auth(client, "folder_move_path")

    async with db_module.async_session() as db:
        doc = await create_document(
            db,
            entity_id,
            name="brief.md",
            fs_path="brief.md",
            file_type="md",
            source="upload",
        )
        doc_id = doc.id
        await db.commit()

    assert await move_path(entity_id, "brief.md", "Projects/brief.md")
    projects_folder_id = await find_folder_path(entity_id, "Projects")
    assert projects_folder_id is not None

    async with db_module.async_session() as db:
        moved = await db.get(Document, doc_id)
        assert moved is not None
        assert moved.fs_path == "Projects/brief.md"
        assert moved.folder_id == projects_folder_id


@pytest.mark.asyncio
async def test_move_path_replaces_an_existing_projection_without_duplicates(
    client: AsyncClient,
):
    import packages.core.database as db_module
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.services.document_service import create_document
    from packages.core.services.knowledge_sync import move_path

    _, entity_id = await _auth(client, "folder_move_replace")
    async with db_module.async_session() as db:
        source = await create_document(
            db,
            entity_id,
            name="source.md",
            fs_path="source.md",
            file_type="md",
            source="upload",
        )
        destination = await create_document(
            db,
            entity_id,
            name="destination.md",
            fs_path="destination.md",
            file_type="md",
            source="upload",
        )
        source_id = source.id
        destination_id = destination.id
        await db.commit()

    assert await move_path(entity_id, "source.md", "destination.md")

    async with db_module.async_session() as db:
        active = list((await db.scalars(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path == "destination.md",
                Document.is_trashed.is_(False),
            )
        )).all())
        replaced = await db.get(Document, destination_id)
        assert [document.id for document in active] == [source_id]
        assert replaced is not None
        assert replaced.is_trashed is True
        assert replaced.fs_path is None
        assert replaced.metadata_["restore_blocked_reason"] == "filesystem_path_replaced"


@pytest.mark.asyncio
async def test_copy_file_projection_replaces_an_existing_destination_identity(
    client: AsyncClient,
):
    import packages.core.database as db_module
    from sqlalchemy import select

    from packages.core.models.document import Document, VectorStatus
    from packages.core.services.document_service import create_document
    from packages.core.services.knowledge_sync import copy_file_projection

    _, entity_id = await _auth(client, "folder_copy_replace")
    async with db_module.async_session() as db:
        await create_document(
            db,
            entity_id,
            name="source.md",
            fs_path="source.md",
            file_type="md",
            source="upload",
            file_size=42,
        )
        destination = await create_document(
            db,
            entity_id,
            name="destination.md",
            fs_path="destination.md",
            file_type="txt",
            source="manual",
            file_size=7,
        )
        destination.vector_status = VectorStatus.READY
        destination_id = destination.id
        await db.commit()

    assert await copy_file_projection(entity_id, "source.md", "destination.md")

    async with db_module.async_session() as db:
        active = list((await db.scalars(
            select(Document).where(
                Document.entity_id == entity_id,
                Document.fs_path == "destination.md",
                Document.is_trashed.is_(False),
            )
        )).all())
        assert len(active) == 1
        assert active[0].id != destination_id
        assert active[0].file_size == 42
        assert active[0].file_type == "md"
        assert active[0].vector_status == VectorStatus.PENDING
        replaced = await db.get(Document, destination_id)
        assert replaced is not None
        assert replaced.is_trashed is True
        assert replaced.fs_path is None
        assert replaced.metadata_["restore_blocked_reason"] == "filesystem_path_replaced"


@pytest.mark.asyncio
async def test_copy_file_projection_preserves_restrictive_security_and_revokes_old_access(
    client: AsyncClient,
):
    import packages.core.database as db_module
    from datetime import datetime, timezone
    from sqlalchemy import select

    from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
    from packages.core.models.permission import (
        Capability,
        GrantStatus,
        PendingStatus,
        ResourceGrant,
        ResourceGrantPending,
        ResourceType,
        Share,
        SubjectType,
    )
    from packages.core.models.user import User
    from packages.core.services.document_service import create_document
    from packages.core.services.knowledge_sync import copy_file_projection

    _, entity_id = await _auth(client, "folder_copy_security")
    async with db_module.async_session() as db:
        owner = await db.scalar(select(User).where(User.entity_id == entity_id))
        assert owner is not None
        source = await create_document(
            db,
            entity_id,
            name="source.md",
            fs_path="source.md",
            file_type="md",
            source="upload",
        )
        source.owner_id = owner.id
        source.created_by = owner.id
        source.visibility = "private"
        source.classification = "restricted"
        source.client_visible = False
        source.pii_detected = True
        source.quarantine_status = "quarantined"
        destination = await create_document(
            db,
            entity_id,
            name="destination.md",
            fs_path="destination.md",
            file_type="txt",
            source="manual",
        )
        destination.visibility = "public"
        destination.classification = "public"
        destination.client_visible = True
        destination.owner_id = "legacy-owner"
        source_group = DocumentGroup(
            entity_id=entity_id,
            name="Source Workspace Knowledge",
            workspace_id="workspace-source",
        )
        destination_group = DocumentGroup(
            entity_id=entity_id,
            name="Destination Workspace Knowledge",
            workspace_id="workspace-destination",
        )
        db.add_all([source_group, destination_group])
        await db.flush()
        db.add_all([
            DocumentGroupMember(document_id=source.id, group_id=source_group.id),
            DocumentGroupMember(document_id=destination.id, group_id=destination_group.id),
        ])
        now = datetime.now(timezone.utc)
        grant = ResourceGrant(
            entity_id=entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=destination.id,
            subject_type=SubjectType.USER,
            subject_id="legacy-reader",
            capabilities=[Capability.VIEW],
            granted_by=owner.id,
            granted_at=now,
            status=GrantStatus.ACTIVE,
        )
        share = Share(
            entity_id=entity_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=destination.id,
            token_hash=f"copy-security-{destination.id}",
            capabilities=[Capability.VIEW],
            created_by=owner.id,
            created_at=now,
            status="active",
        )
        share_approval = ResourceGrantPending(
            entity_id=entity_id,
            resource_type="share",
            resource_id=destination.id,
            requester_user_id=owner.id,
            requested_capabilities=[Capability.VIEW],
            status=PendingStatus.PENDING,
        )
        db.add_all([grant, share, share_approval])
        await db.flush()
        owner_id = owner.id
        destination_id = destination.id
        grant_id = grant.id
        share_id = share.id
        share_approval_id = share_approval.id
        source_group_id = source_group.id
        destination_group_id = destination_group.id
        await db.commit()

    assert await copy_file_projection(entity_id, "source.md", "destination.md")
    assert await copy_file_projection(entity_id, "source.md", "new-copy.md")

    async with db_module.async_session() as db:
        replaced_destination = await db.get(Document, destination_id)
        destination = await db.scalar(select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == "destination.md",
            Document.is_trashed.is_(False),
        ))
        copied = await db.scalar(select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == "new-copy.md",
            Document.is_trashed.is_(False),
        ))
        assert replaced_destination is not None
        assert replaced_destination.is_trashed is True
        assert destination is not None
        assert destination.id != destination_id
        assert copied is not None
        for document in (destination, copied):
            assert document.owner_id == owner_id
            assert document.visibility == "private"
            assert document.classification == "restricted"
            assert document.client_visible is False
            assert document.pii_detected is True
            assert document.quarantine_status == "quarantined"
        assert (await db.get(ResourceGrant, grant_id)).status == GrantStatus.REVOKED
        assert (await db.get(Share, share_id)).status == "revoked"
        assert (
            await db.get(ResourceGrantPending, share_approval_id)
        ).status == PendingStatus.DENIED
        destination_group_ids = set((await db.scalars(
            select(DocumentGroupMember.group_id).where(
                DocumentGroupMember.document_id == destination.id,
            )
        )).all())
        assert destination_group_ids == {source_group_id}
        assert destination_group_id not in destination_group_ids


@pytest.mark.asyncio
async def test_move_path_rolls_back_folder_projection_on_failure(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as db_module

    from packages.core.models.document import Document
    from packages.core.services import knowledge_sync
    from packages.core.services.document_service import create_document

    _, entity_id = await _auth(client, "folder_move_rollback")
    source_folder_id = await knowledge_sync.ensure_folder_path(entity_id, "Source")
    assert source_folder_id is not None
    async with db_module.async_session() as db:
        document = await create_document(
            db,
            entity_id,
            name="brief.md",
            fs_path="Source/brief.md",
            file_type="md",
            source="upload",
            folder_id=source_folder_id,
        )
        document_id = document.id
        await db.commit()

    original_move = knowledge_sync._move_folder_path_in_session

    async def fail_after_folder_move(*args, **kwargs):
        assert await original_move(*args, **kwargs)
        raise RuntimeError("projection failure")

    monkeypatch.setattr(
        knowledge_sync,
        "_move_folder_path_in_session",
        fail_after_folder_move,
    )
    with pytest.raises(RuntimeError, match="projection failure"):
        await knowledge_sync.move_path(entity_id, "Source", "Renamed")

    assert await knowledge_sync.find_folder_path(entity_id, "Source") == source_folder_id
    assert await knowledge_sync.find_folder_path(entity_id, "Renamed") is None
    async with db_module.async_session() as db:
        document = await db.get(Document, document_id)
        assert document is not None
        assert document.fs_path == "Source/brief.md"
        assert document.folder_id == source_folder_id
