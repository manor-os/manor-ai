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
