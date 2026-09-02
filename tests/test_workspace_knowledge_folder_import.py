"""Workspace Knowledge folder import regression tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.workspace import WorkspaceStaff
from packages.core.services.document_service import add_documents_to_group, list_documents
from tests.test_document_permissions import _create_entity_user


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_add_knowledge_folder_to_workspace_is_recursive_and_idempotent(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "workspace_folder_import")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Folder Import"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    parent_response = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Product Docs"},
    )
    assert parent_response.status_code == 201, parent_response.text
    parent_id = parent_response.json()["id"]
    child_response = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Archive", "parent_id": parent_id},
    )
    assert child_response.status_code == 201, child_response.text
    child_id = child_response.json()["id"]

    direct_response = await client.post(
        f"/api/v1/documents/upload?folder_id={parent_id}",
        headers=headers,
        files={"file": ("overview.md", b"# Overview", "text/markdown")},
    )
    nested_response = await client.post(
        f"/api/v1/documents/upload?folder_id={child_id}",
        headers=headers,
        files={"file": ("history.md", b"# History", "text/markdown")},
    )
    outside_response = await client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": ("outside.md", b"# Outside", "text/markdown")},
    )
    assert direct_response.status_code == 201, direct_response.text
    assert nested_response.status_code == 201, nested_response.text
    assert outside_response.status_code == 201, outside_response.text
    direct_id = direct_response.json()["id"]
    nested_id = nested_response.json()["id"]
    outside_id = outside_response.json()["id"]

    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{parent_id}",
        headers=headers,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body == {
        "group_id": first_body["group_id"],
        "group_name": "Product Docs",
        "created": True,
        "added": 2,
        "existing": 0,
        "total": 2,
    }

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    imported_group = next(
        group for group in listed.json() if group["id"] == first_body["group_id"]
    )
    assert {document["id"] for document in imported_group["documents"]} == {
        direct_id,
        nested_id,
    }
    assert outside_id not in {
        document["id"] for document in imported_group["documents"]
    }

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{parent_id}",
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json() == {
        "group_id": first_body["group_id"],
        "group_name": "Product Docs",
        "created": False,
        "added": 0,
        "existing": 2,
        "total": 2,
    }

    new_nested_response = await client.post(
        f"/api/v1/documents/upload?folder_id={child_id}",
        headers=headers,
        files={"file": ("roadmap.md", b"# Roadmap", "text/markdown")},
    )
    assert new_nested_response.status_code == 201, new_nested_response.text
    third = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{parent_id}",
        headers=headers,
    )
    assert third.status_code == 200, third.text
    assert third.json()["group_id"] == first_body["group_id"]
    assert third.json()["created"] is False
    assert third.json()["added"] == 1
    assert third.json()["existing"] == 2
    assert third.json()["total"] == 3

    db_session.expire_all()
    group = await db_session.get(DocumentGroup, first_body["group_id"])
    assert group is not None
    assert group.settings["knowledge_folder_source"] == {
        "folder_id": parent_id,
        "mode": "recursive_snapshot",
    }
    member_ids = set((await db_session.execute(
        select(DocumentGroupMember.document_id).where(
            DocumentGroupMember.group_id == group.id,
        )
    )).scalars().all())
    assert member_ids == {direct_id, nested_id, new_nested_response.json()["id"]}
    assert (await db_session.get(Document, direct_id)).folder_id == parent_id
    assert (await db_session.get(Document, nested_id)).folder_id == child_id


@pytest.mark.asyncio
async def test_add_knowledge_folder_rejects_a_folder_from_another_entity(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "workspace_folder_owner")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Scoped Folder Import"},
    )
    assert workspace_response.status_code == 201, workspace_response.text

    other_headers = await _auth(client, "workspace_folder_other")
    foreign_folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=other_headers,
        json={"name": "Foreign Folder"},
    )
    assert foreign_folder_response.status_code == 201, foreign_folder_response.text

    response = await client.post(
        "/api/v1/workspaces/"
        f"{workspace_response.json()['id']}/documents/folders/"
        f"{foreign_folder_response.json()['id']}",
        headers=owner_headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_workspace_owner_must_also_manage_the_source_folder(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "workspace_folder_source_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert owner_response.status_code == 200, owner_response.text
    owner = owner_response.json()

    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Folder Permission Target"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    shared_folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": "Shared Source"},
    )
    assert shared_folder_response.status_code == 201, shared_folder_response.text
    shared_folder_id = shared_folder_response.json()["id"]
    properties_response = await client.post(
        f"/api/v1/folders/{shared_folder_id}/properties",
        headers=owner_headers,
        json={"visibility": "entity"},
    )
    assert properties_response.status_code == 200, properties_response.text
    shared_document_response = await client.post(
        f"/api/v1/documents/upload?folder_id={shared_folder_id}",
        headers=owner_headers,
        files={"file": ("shared.md", b"# Shared", "text/markdown")},
    )
    assert shared_document_response.status_code == 201, shared_document_response.text

    member = await _create_entity_user(
        owner["entity_id"],
        "workspace_folder_member_owner",
        "member",
    )
    async with db_module.async_session() as session:
        session.add(WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=member["id"],
            role="owner",
            status="active",
        ))
        await session.commit()

    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{shared_folder_id}",
        headers=member["headers"],
    )
    assert denied.status_code == 403, denied.text

    async with db_module.async_session() as session:
        session.add(ResourceGrant(
            entity_id=owner["entity_id"],
            resource_type=ResourceType.DOCUMENT_FOLDER,
            resource_id=shared_folder_id,
            subject_type=SubjectType.USER,
            subject_id=member["id"],
            capabilities=[Capability.MANAGE_METADATA],
            granted_by=owner["id"],
            granted_at=datetime.now(timezone.utc),
            status=GrantStatus.ACTIVE,
        ))
        await session.commit()

    granted = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{shared_folder_id}",
        headers=member["headers"],
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["added"] == 1

    owned_folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=member["headers"],
        json={"name": "Member Source"},
    )
    assert owned_folder_response.status_code == 201, owned_folder_response.text
    owned_folder_id = owned_folder_response.json()["id"]
    owned_document_response = await client.post(
        f"/api/v1/documents/upload?folder_id={owned_folder_id}",
        headers=member["headers"],
        files={"file": ("member.md", b"# Member", "text/markdown")},
    )
    assert owned_document_response.status_code == 201, owned_document_response.text

    allowed = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{owned_folder_id}",
        headers=member["headers"],
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["added"] == 1

    workspace_list = await client.get("/api/v1/workspaces", headers=member["headers"])
    assert workspace_list.status_code == 200, workspace_list.text
    listed_workspace = next(
        workspace
        for workspace in workspace_list.json()
        if workspace["id"] == workspace_id
    )
    assert listed_workspace["can_manage"] is True


@pytest.mark.asyncio
async def test_folder_import_rejects_read_only_descendant_documents(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "workspace_folder_document_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert owner_response.status_code == 200, owner_response.text
    owner = owner_response.json()

    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Read-only Document Target"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    member = await _create_entity_user(
        owner["entity_id"],
        "workspace_folder_read_only_importer",
        "member",
    )
    async with db_module.async_session() as session:
        session.add(WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=member["id"],
            role="owner",
            status="active",
        ))
        await session.commit()

    folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=member["headers"],
        json={"name": "Mixed Ownership Source"},
    )
    assert folder_response.status_code == 201, folder_response.text
    folder_id = folder_response.json()["id"]

    async with db_module.async_session() as session:
        session.add(Document(
            entity_id=owner["entity_id"],
            name="read-only.md",
            folder_id=folder_id,
            owner_id=owner["id"],
            visibility="entity",
        ))
        await session.commit()

    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{folder_id}",
        headers=member["headers"],
    )
    assert denied.status_code == 403, denied.text

    async with db_module.async_session() as session:
        imported_groups = list((await session.execute(
            select(DocumentGroup).where(
                DocumentGroup.workspace_id == workspace_id,
                DocumentGroup.entity_id == owner["entity_id"],
            )
        )).scalars().all())
    assert all(
        (group.settings or {}).get("knowledge_folder_source", {}).get("folder_id")
        != folder_id
        for group in imported_groups
    )


@pytest.mark.asyncio
async def test_folder_import_revalidates_document_capability_before_commit(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.services import document_access

    headers = await _auth(client, "workspace_folder_revoked_during_scan")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Revoked Folder Import"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Revoked Source"},
    )
    assert folder_response.status_code == 201, folder_response.text
    folder_id = folder_response.json()["id"]
    upload_response = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=headers,
        files={"file": ("revoked.md", b"# Revoked", "text/markdown")},
    )
    assert upload_response.status_code == 201, upload_response.text

    revalidation_calls = []

    async def revoked_capability(_db, documents, **_kwargs):
        revalidation_calls.append([document.id for document in documents])
        return [], documents

    monkeypatch.setattr(
        document_access,
        "partition_documents_by_capability",
        revoked_capability,
    )
    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{folder_id}",
        headers=headers,
    )
    assert denied.status_code == 403, denied.text
    assert revalidation_calls == [[upload_response.json()["id"]]]

    db_session.expire_all()
    imported_groups = list((await db_session.execute(
        select(DocumentGroup).where(
            DocumentGroup.workspace_id == workspace_id,
            DocumentGroup.entity_id == workspace_response.json()["entity_id"],
        )
    )).scalars().all())
    assert all(
        (group.settings or {}).get("knowledge_folder_source", {}).get("folder_id")
        != folder_id
        for group in imported_groups
    )


@pytest.mark.asyncio
async def test_folder_import_serializes_permission_revocation_after_revalidation(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.services import document_access

    owner_headers = await _auth(client, "workspace_folder_revoke_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert owner_response.status_code == 200, owner_response.text
    owner = owner_response.json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Serialized Folder Import"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": "Serialized Source"},
    )
    assert folder_response.status_code == 201, folder_response.text
    folder_id = folder_response.json()["id"]

    member = await _create_entity_user(
        owner["entity_id"],
        "workspace_folder_revoke_member",
        "member",
    )
    async with db_module.async_session() as session:
        document = Document(
            entity_id=owner["entity_id"],
            name="serialized.md",
            folder_id=folder_id,
            owner_id=owner["id"],
            visibility="entity",
        )
        grant = ResourceGrant(
            entity_id=owner["entity_id"],
            resource_type=ResourceType.DOCUMENT_FOLDER,
            resource_id=folder_id,
            subject_type=SubjectType.USER,
            subject_id=member["id"],
            capabilities=[Capability.MANAGE_METADATA],
            granted_by=owner["id"],
            granted_at=datetime.now(timezone.utc),
            status=GrantStatus.ACTIVE,
        )
        workspace_membership = WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=member["id"],
            role="owner",
            status="active",
        )
        session.add_all([
            workspace_membership,
            document,
            grant,
        ])
        await session.flush()
        document_id = document.id
        grant_id = grant.id
        membership_id = workspace_membership.id
        await session.commit()

    revalidated = asyncio.Event()
    release_import = asyncio.Event()
    original_partition = document_access.partition_documents_by_capability

    async def pause_after_revalidation(*args, **kwargs):
        result = await original_partition(*args, **kwargs)
        revalidated.set()
        await release_import.wait()
        return result

    monkeypatch.setattr(
        document_access,
        "partition_documents_by_capability",
        pause_after_revalidation,
    )
    import_task = asyncio.create_task(client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{folder_id}",
        headers=member["headers"],
    ))
    await asyncio.wait_for(revalidated.wait(), timeout=5)

    async def revoke_grant() -> None:
        async with db_module.async_session() as session:
            active_grant = await session.get(ResourceGrant, grant_id)
            assert active_grant is not None
            active_grant.status = GrantStatus.REVOKED
            await session.commit()

    async def revoke_workspace_membership() -> None:
        async with db_module.async_session() as session:
            membership = await session.get(WorkspaceStaff, membership_id)
            assert membership is not None
            await session.delete(membership)
            await session.commit()

    revoke_task = asyncio.create_task(revoke_grant())
    membership_revoke_task = asyncio.create_task(revoke_workspace_membership())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.2)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(membership_revoke_task), timeout=0.2)

    release_import.set()
    imported = await import_task
    assert imported.status_code == 200, imported.text
    await asyncio.wait_for(
        asyncio.gather(revoke_task, membership_revoke_task),
        timeout=5,
    )

    async with db_module.async_session() as session:
        assert await session.scalar(
            select(ResourceGrant.status).where(ResourceGrant.id == grant_id)
        ) == GrantStatus.REVOKED
        assert await session.get(WorkspaceStaff, membership_id) is None
        members = set((await session.execute(
            select(DocumentGroupMember.document_id)
            .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
            .where(
                DocumentGroup.workspace_id == workspace_id,
                DocumentGroup.entity_id == owner["entity_id"],
            )
        )).scalars().all())
    assert members == {document_id}


@pytest.mark.asyncio
async def test_folder_import_revalidates_documents_in_bounded_batches(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.services import document_access

    headers = await _auth(client, "workspace_folder_bounded_revalidation")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Bounded Folder Import"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    folder_response = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Bounded Source"},
    )
    assert folder_response.status_code == 201, folder_response.text
    folder_id = folder_response.json()["id"]

    async with db_module.async_session() as session:
        session.add_all([
            Document(
                entity_id=me["entity_id"],
                name=f"bounded-{index}.md",
                folder_id=folder_id,
                owner_id=me["id"],
            )
            for index in range(501)
        ])
        await session.commit()

    batch_sizes: list[int] = []
    original_partition = document_access.partition_documents_by_capability

    async def record_batch_size(db, documents, **kwargs):
        batch_sizes.append(len(documents))
        return await original_partition(db, documents, **kwargs)

    monkeypatch.setattr(
        document_access,
        "partition_documents_by_capability",
        record_batch_size,
    )
    imported = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/folders/{folder_id}",
        headers=headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["added"] == 501
    assert batch_sizes == [500, 1]


@pytest.mark.asyncio
async def test_group_member_bulk_insert_is_concurrency_safe(
    client: AsyncClient,
):
    headers = await _auth(client, "workspace_folder_concurrency")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()

    async with db_module.async_session() as session:
        document = Document(
            entity_id=me["entity_id"],
            name="concurrent.md",
            owner_id=me["id"],
        )
        group = DocumentGroup(
            entity_id=me["entity_id"],
            name="Concurrent Net",
        )
        session.add_all([document, group])
        await session.flush()
        document_id = document.id
        group_id = group.id
        await session.commit()

    async def attach_once() -> int:
        async with db_module.async_session() as session:
            added = await add_documents_to_group(
                session,
                [document_id],
                group_id,
                entity_id=me["entity_id"],
            )
            await session.commit()
            return added

    assert sorted(await asyncio.gather(attach_once(), attach_once())) == [0, 1]


@pytest.mark.asyncio
async def test_workspace_document_attach_requires_metadata_management_across_entries(
    client: AsyncClient,
):
    from packages.core.ai.runtime.workspace_knowledge_actions import (
        runtime_workspace_add_knowledge_documents_action,
    )

    owner_headers = await _auth(client, "workspace_attach_owner")
    owner_response = await client.get("/api/v1/auth/me", headers=owner_headers)
    assert owner_response.status_code == 200, owner_response.text
    owner = owner_response.json()

    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Document Attach Authorization"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    member = await _create_entity_user(
        owner["entity_id"],
        "workspace_attach_member",
        "member",
    )
    async with db_module.async_session() as session:
        session.add(WorkspaceStaff(
            workspace_id=workspace_id,
            user_id=member["id"],
            role="owner",
            status="active",
        ))
        api_document = Document(
            entity_id=owner["entity_id"],
            name="api-read-only.md",
            owner_id=owner["id"],
            visibility="entity",
        )
        runtime_document = Document(
            entity_id=owner["entity_id"],
            name="runtime-read-only.md",
            owner_id=owner["id"],
            visibility="entity",
        )
        session.add_all([api_document, runtime_document])
        await session.flush()
        api_document_id = api_document.id
        runtime_document_id = runtime_document.id
        await session.commit()

    group_response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups",
        headers=member["headers"],
        json={"name": "Authorized Attachments"},
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["id"]

    api_denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}/members",
        headers=member["headers"],
        json={"document_ids": [api_document_id]},
    )
    assert api_denied.status_code == 200, api_denied.text
    assert api_denied.json() == {
        "added": 0,
        "skipped": [api_document_id],
        "total": 1,
    }

    runtime_denied = json.loads(
        await runtime_workspace_add_knowledge_documents_action(
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            user_id=member["id"],
            params={"document_ids": [runtime_document_id], "group_id": group_id},
        )
    )
    assert runtime_denied["error"] == "no_documents_authorized"

    async with db_module.async_session() as session:
        attached_before_grant = set((await session.execute(
            select(DocumentGroupMember.document_id).where(
                DocumentGroupMember.group_id == group_id,
            )
        )).scalars().all())
        assert not attached_before_grant
        session.add_all([
            ResourceGrant(
                entity_id=owner["entity_id"],
                resource_type=ResourceType.DOCUMENT,
                resource_id=document_id,
                subject_type=SubjectType.USER,
                subject_id=member["id"],
                capabilities=[Capability.MANAGE_METADATA],
                granted_by=owner["id"],
                granted_at=datetime.now(timezone.utc),
                status=GrantStatus.ACTIVE,
            )
            for document_id in (api_document_id, runtime_document_id)
        ])
        await session.commit()

    api_allowed = await client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/groups/{group_id}/members",
        headers=member["headers"],
        json={"document_ids": [api_document_id]},
    )
    assert api_allowed.status_code == 200, api_allowed.text
    assert api_allowed.json()["added"] == 1

    runtime_allowed = json.loads(
        await runtime_workspace_add_knowledge_documents_action(
            entity_id=owner["entity_id"],
            workspace_id=workspace_id,
            user_id=member["id"],
            params={"document_ids": [runtime_document_id], "group_id": group_id},
        )
    )
    assert runtime_allowed["updated"] is True
    assert [row["document_id"] for row in runtime_allowed["added"]] == [
        runtime_document_id,
    ]


@pytest.mark.asyncio
async def test_document_listing_uses_a_unique_pagination_order(client: AsyncClient):
    headers = await _auth(client, "workspace_folder_stable_paging")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200, me_response.text
    entity_id = me_response.json()["entity_id"]
    owner_id = me_response.json()["id"]

    ids = [generate_ulid() for _ in range(3)]
    insertion_order = [ids[1], ids[0], ids[2]]
    created_at = datetime.now(timezone.utc)
    async with db_module.async_session() as session:
        session.add_all([
            Document(
                id=document_id,
                entity_id=entity_id,
                name=f"{document_id}.md",
                owner_id=owner_id,
                created_at=created_at,
            )
            for document_id in insertion_order
        ])
        await session.commit()
        first_page, _ = await list_documents(session, entity_id, limit=2, offset=0)
        second_page, _ = await list_documents(session, entity_id, limit=2, offset=2)

    assert [document.id for document in first_page + second_page] == sorted(
        ids,
        reverse=True,
    )


@pytest.mark.asyncio
async def test_workspace_list_batches_manageability(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    import apps.api.routers.workspaces as workspaces_router

    headers = await _auth(client, "workspace_manageability_batch")
    for name in ("Batch One", "Batch Two", "Batch Three"):
        created = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": name},
        )
        assert created.status_code == 201, created.text

    original = workspaces_router.manageable_workspace_ids_for_user
    calls = 0

    async def tracked(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    async def unexpected_per_workspace_check(*args, **kwargs):
        raise AssertionError("workspace list must not check management one row at a time")

    monkeypatch.setattr(
        workspaces_router,
        "manageable_workspace_ids_for_user",
        tracked,
    )
    monkeypatch.setattr(
        workspaces_router,
        "user_can_manage_workspace",
        unexpected_per_workspace_check,
    )

    response = await client.get("/api/v1/workspaces", headers=headers)
    assert response.status_code == 200, response.text
    assert calls == 1
    assert all(workspace["can_manage"] is True for workspace in response.json())
