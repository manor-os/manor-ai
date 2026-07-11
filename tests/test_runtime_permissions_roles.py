"""Role and resource-ACL coverage for workspace/document runtime permissions."""

from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.ai.runtime.document_actions import runtime_list_documents_action
from packages.core.ai.runtime.rag import runtime_rag_action
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
    Visibility,
)
from packages.core.models.task import Conversation, Task
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.auth_service import create_access_token, hash_password


async def _register_owner(client: AsyncClient, username: str = "perm_owner") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Permission Corp",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    data["headers"] = {"Authorization": f"Bearer {data['access_token']}"}
    return data


async def _create_entity_user(entity_id: str, username: str, role: str) -> dict:
    async with db_module.async_session() as session:
        user = User(
            entity_id=entity_id,
            email=f"{username}@test.com",
            display_name=username,
            password_hash=hash_password("pass123"),
            role=role,
            status="active",
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()
    token = create_access_token(user_id, entity_id, role)
    return {
        "id": user_id,
        "headers": {"Authorization": f"Bearer {token}"},
        "role": role,
    }


async def _add_workspace_staff(workspace_id: str, user_id: str, role: str, added_by: str) -> None:
    async with db_module.async_session() as session:
        session.add(
            WorkspaceStaff(
                workspace_id=workspace_id,
                user_id=user_id,
                role=role,
                added_by=added_by,
                added_at=datetime.now(UTC),
                status="active",
            )
        )
        await session.commit()


async def _seed_document(
    *,
    entity_id: str,
    owner_id: str,
    name: str,
    visibility: str = Visibility.ENTITY,
    workspace_id: str | None = None,
    classification: str = "internal",
    client_visible: bool = False,
) -> str:
    async with db_module.async_session() as session:
        doc = Document(
            entity_id=entity_id,
            name=name,
            file_type="md",
            mime_type="text/markdown",
            source="test",
            created_by=owner_id,
            owner_id=owner_id,
            visibility=visibility,
            classification=classification,
            client_visible=client_visible,
            metadata_={"content": name},
        )
        session.add(doc)
        await session.flush()
        if workspace_id:
            group = DocumentGroup(
                entity_id=entity_id,
                workspace_id=workspace_id,
                name=f"{name} group",
                settings={"kind": "knowledge_net", "user_manageable": True},
            )
            session.add(group)
            await session.flush()
            session.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
        doc_id = doc.id
        await session.commit()
    return doc_id


async def _grant_document_caps(
    entity_id: str,
    doc_id: str,
    user_id: str,
    granted_by: str,
    capabilities: list[str],
) -> None:
    async with db_module.async_session() as session:
        session.add(
            ResourceGrant(
                entity_id=entity_id,
                resource_type=ResourceType.DOCUMENT,
                resource_id=doc_id,
                subject_type=SubjectType.USER,
                subject_id=user_id,
                capabilities=capabilities,
                granted_by=granted_by,
                granted_at=datetime.now(UTC),
                status=GrantStatus.ACTIVE,
            )
        )
        await session.commit()


async def _grant_document_view(entity_id: str, doc_id: str, user_id: str, granted_by: str) -> None:
    await _grant_document_caps(entity_id, doc_id, user_id, granted_by, [Capability.VIEW])


async def _grant_folder_caps(
    entity_id: str,
    folder_id: str,
    user_id: str,
    granted_by: str,
    capabilities: list[str],
) -> None:
    async with db_module.async_session() as session:
        session.add(
            ResourceGrant(
                entity_id=entity_id,
                resource_type=ResourceType.DOCUMENT_FOLDER,
                resource_id=folder_id,
                subject_type=SubjectType.USER,
                subject_id=user_id,
                capabilities=capabilities,
                granted_by=granted_by,
                granted_at=datetime.now(UTC),
                status=GrantStatus.ACTIVE,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_workspace_members_only_role_matrix(client: AsyncClient):
    owner = await _register_owner(client, "workspace_owner")
    entity_id = owner["entity_id"]
    admin = await _create_entity_user(entity_id, "workspace_admin", "admin")
    member = await _create_entity_user(entity_id, "workspace_member", "member")
    ws_owner = await _create_entity_user(entity_id, "workspace_local_owner", "member")
    client_user = await _create_entity_user(entity_id, "workspace_client", "client")

    create_resp = await client.post(
        "/api/v1/workspaces",
        headers=owner["headers"],
        json={"name": "Members Only"},
    )
    assert create_resp.status_code == 201, create_resp.text
    workspace = create_resp.json()
    workspace_id = workspace["id"]
    assert workspace["settings"]["access_mode"] == "members_only"

    assert (await client.get(f"/api/v1/workspaces/{workspace_id}", headers=admin["headers"])).status_code == 200
    assert (await client.get(f"/api/v1/workspaces/{workspace_id}", headers=member["headers"])).status_code == 404
    assert (await client.get("/api/v1/workspaces", headers=member["headers"])).json() == []

    search_needle = "MembersOnlySearchNeedle"
    async with db_module.async_session() as session:
        session.add(
            Task(
                entity_id=entity_id,
                workspace_id=workspace_id,
                title=f"{search_needle} private task",
                description="Should not leak to non-members",
                creator_id=owner["user_id"],
            )
        )
        session.add(
            Conversation(
                entity_id=entity_id,
                workspace_id=workspace_id,
                title=f"{search_needle} private thread",
                channel="workspace",
                scope="workspace_main",
            )
        )
        await session.commit()
    hidden_search = await client.get(
        f"/api/v1/search?q={search_needle}",
        headers=member["headers"],
    )
    assert hidden_search.status_code == 200
    assert hidden_search.json()["tasks"] == []
    assert hidden_search.json()["conversations"] == []
    assert (await client.get(f"/api/v1/workspaces/{workspace_id}/staff", headers=member["headers"])).status_code == 404
    assert (
        await client.put(
            f"/api/v1/workspaces/{workspace_id}/goals",
            headers=member["headers"],
            json={"goals": []},
        )
    ).status_code == 404

    await _add_workspace_staff(workspace_id, member["id"], "viewer", owner["user_id"])
    member_get = await client.get(f"/api/v1/workspaces/{workspace_id}", headers=member["headers"])
    assert member_get.status_code == 200
    assert [row["id"] for row in (await client.get("/api/v1/workspaces", headers=member["headers"])).json()] == [
        workspace_id
    ]
    visible_search = await client.get(
        f"/api/v1/search?q={search_needle}",
        headers=member["headers"],
    )
    assert [row["name"] for row in visible_search.json()["tasks"]] == [f"{search_needle} private task"]
    assert [row["name"] for row in visible_search.json()["conversations"]] == [f"{search_needle} private thread"]

    forbidden_update = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        headers=member["headers"],
        json={"name": "Viewer Rename"},
    )
    assert forbidden_update.status_code == 403

    await _add_workspace_staff(workspace_id, ws_owner["id"], "owner", owner["user_id"])
    allowed_update = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        headers=ws_owner["headers"],
        json={"name": "Workspace Owner Rename"},
    )
    assert allowed_update.status_code == 200
    assert allowed_update.json()["name"] == "Workspace Owner Rename"

    async with db_module.async_session() as session:
        entity_visible = Workspace(
            entity_id=entity_id,
            name="Entity Visible Internal Workspace",
            settings={"access_mode": "entity_visible"},
        )
        session.add(entity_visible)
        await session.flush()
        entity_visible_id = entity_visible.id
        await session.commit()
    assert (await client.get(f"/api/v1/workspaces/{entity_visible_id}", headers=member["headers"])).status_code == 200
    assert (
        await client.get(f"/api/v1/workspaces/{entity_visible_id}", headers=client_user["headers"])
    ).status_code == 404
    await _add_workspace_staff(entity_visible_id, client_user["id"], "external_client", owner["user_id"])
    assert (
        await client.get(f"/api/v1/workspaces/{entity_visible_id}", headers=client_user["headers"])
    ).status_code == 200


@pytest.mark.asyncio
async def test_document_visibility_and_grants_role_matrix(client: AsyncClient):
    owner = await _register_owner(client, "doc_owner")
    entity_id = owner["entity_id"]
    member = await _create_entity_user(entity_id, "doc_member", "member")
    viewer = await _create_entity_user(entity_id, "doc_viewer", "viewer")

    workspace_resp = await client.post(
        "/api/v1/workspaces",
        headers=owner["headers"],
        json={"name": "Doc Workspace"},
    )
    workspace_id = workspace_resp.json()["id"]
    await _add_workspace_staff(workspace_id, viewer["id"], "viewer", owner["user_id"])

    entity_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="entity-visible.md",
    )
    private_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="private-only.md",
        visibility=Visibility.PRIVATE,
    )
    workspace_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="workspace-only.md",
        visibility=Visibility.WORKSPACE,
        workspace_id=workspace_id,
    )

    assert (await client.get(f"/api/v1/documents/{entity_doc_id}", headers=member["headers"])).status_code == 200
    assert (await client.get(f"/api/v1/documents/{private_doc_id}", headers=member["headers"])).status_code == 404
    assert (await client.get(f"/api/v1/documents/{workspace_doc_id}", headers=member["headers"])).status_code == 404
    assert (await client.get(f"/api/v1/documents/{workspace_doc_id}", headers=viewer["headers"])).status_code == 200

    member_list = (await client.get("/api/v1/documents", headers=member["headers"])).json()
    assert {item["id"] for item in member_list["items"]} == {entity_doc_id}

    await _grant_document_view(entity_id, private_doc_id, member["id"], owner["user_id"])
    granted_get = await client.get(f"/api/v1/documents/{private_doc_id}", headers=member["headers"])
    assert granted_get.status_code == 200

    viewer_delete = await client.delete(f"/api/v1/documents/{entity_doc_id}", headers=viewer["headers"])
    assert viewer_delete.status_code == 403


@pytest.mark.asyncio
async def test_document_grant_capabilities_gate_view_edit_and_delete(client: AsyncClient):
    owner = await _register_owner(client, "doc_caps_owner")
    entity_id = owner["entity_id"]
    member = await _create_entity_user(entity_id, "doc_caps_member", "member")

    doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="private-capability.md",
        visibility=Visibility.PRIVATE,
    )

    blocked_get = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert blocked_get.status_code == 404

    await _grant_document_caps(
        entity_id,
        doc_id,
        member["id"],
        owner["user_id"],
        [Capability.VIEW],
    )
    view_only_get = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert view_only_get.status_code == 200
    assert "view" in view_only_get.json()["current_user_capabilities"]
    assert "edit" not in view_only_get.json()["current_user_capabilities"]

    view_only_save = await client.put(
        f"/api/v1/documents/{doc_id}/content",
        headers=member["headers"],
        json={"content": "view-only update"},
    )
    assert view_only_save.status_code == 403

    await _grant_document_caps(
        entity_id,
        doc_id,
        member["id"],
        owner["user_id"],
        [Capability.VIEW, Capability.COMMENT, Capability.EDIT],
    )
    editor_get = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert editor_get.status_code == 200
    assert set(editor_get.json()["current_user_capabilities"]) >= {"view", "comment", "edit"}
    assert "delete" not in editor_get.json()["current_user_capabilities"]

    editor_save = await client.put(
        f"/api/v1/documents/{doc_id}/content",
        headers=member["headers"],
        json={"content": "editor update"},
    )
    assert editor_save.status_code == 200, editor_save.text
    content_get = await client.get(f"/api/v1/documents/{doc_id}/content", headers=member["headers"])
    assert content_get.status_code == 200
    assert content_get.json()["content"] == "editor update"

    editor_rename = await client.put(
        f"/api/v1/documents/{doc_id}",
        headers=member["headers"],
        json={"name": "editor-renamed.md"},
    )
    assert editor_rename.status_code == 403
    editor_delete = await client.delete(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert editor_delete.status_code == 403


@pytest.mark.asyncio
async def test_document_curator_can_manage_acl_and_metadata_without_delete(client: AsyncClient):
    owner = await _register_owner(client, "doc_curator_owner")
    entity_id = owner["entity_id"]
    curator = await _create_entity_user(entity_id, "doc_curator_member", "member")
    viewer = await _create_entity_user(entity_id, "doc_curator_viewer", "viewer")

    doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="curated-private.md",
        visibility=Visibility.PRIVATE,
    )

    grant_resp = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=owner["headers"],
        json={
            "subject_type": "user",
            "subject_id": curator["id"],
            "capabilities": [
                Capability.VIEW,
                Capability.COMMENT,
                Capability.EDIT,
                Capability.MANAGE_METADATA,
                Capability.GRANT_ACCESS,
                Capability.SHARE_INTERNAL,
            ],
        },
    )
    assert grant_resp.status_code == 201, grant_resp.text

    curator_rename = await client.put(
        f"/api/v1/documents/{doc_id}",
        headers=curator["headers"],
        json={"name": "curator-renamed.md"},
    )
    assert curator_rename.status_code == 200, curator_rename.text
    assert curator_rename.json()["name"] == "curator-renamed.md"
    assert "manage_metadata" in curator_rename.json()["current_user_capabilities"]
    assert "grant_access" in curator_rename.json()["current_user_capabilities"]
    assert "delete" not in curator_rename.json()["current_user_capabilities"]

    curator_grants = await client.get(f"/api/v1/documents/{doc_id}/grants", headers=curator["headers"])
    assert curator_grants.status_code == 200, curator_grants.text

    viewer_grant = await client.post(
        f"/api/v1/documents/{doc_id}/grants",
        headers=curator["headers"],
        json={
            "subject_type": "user",
            "subject_id": viewer["id"],
            "capabilities": [Capability.VIEW],
        },
    )
    assert viewer_grant.status_code == 201, viewer_grant.text
    viewer_get = await client.get(f"/api/v1/documents/{doc_id}", headers=viewer["headers"])
    assert viewer_get.status_code == 200
    assert "view" in viewer_get.json()["current_user_capabilities"]

    curator_delete = await client.delete(f"/api/v1/documents/{doc_id}", headers=curator["headers"])
    assert curator_delete.status_code == 403


@pytest.mark.asyncio
async def test_folder_editor_grant_cascades_to_child_edit_and_upload(client: AsyncClient):
    owner = await _register_owner(client, "folder_caps_owner")
    entity_id = owner["entity_id"]
    member = await _create_entity_user(entity_id, "folder_caps_member", "member")

    folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=owner["headers"],
        json={"name": "Shared Folder"},
    )
    assert folder_resp.status_code == 201, folder_resp.text
    folder_id = folder_resp.json()["id"]

    upload_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}&visibility=private",
        headers=owner["headers"],
        files={"file": ("folder-child.md", b"initial", "text/markdown")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    doc_id = upload_resp.json()["id"]

    before_grant = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert before_grant.status_code == 404

    await _grant_folder_caps(
        entity_id,
        folder_id,
        member["id"],
        owner["user_id"],
        [
            Capability.VIEW,
            Capability.COMMENT,
            Capability.EDIT,
            Capability.UPLOAD_TO,
            Capability.GRANT_ACCESS,
            Capability.SHARE_INTERNAL,
        ],
    )

    member_folders = await client.get("/api/v1/documents/folders", headers=member["headers"])
    assert member_folders.status_code == 200, member_folders.text
    shared_folder = next(folder for folder in member_folders.json() if folder["id"] == folder_id)
    assert set(shared_folder["current_user_capabilities"]) >= {
        "view",
        "comment",
        "edit",
        "upload_to",
        "grant_access",
        "share_internal",
    }
    assert "delete" not in shared_folder["current_user_capabilities"]

    member_get = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert member_get.status_code == 200, member_get.text
    assert set(member_get.json()["current_user_capabilities"]) >= {"view", "comment", "edit", "upload_to"}

    member_save = await client.put(
        f"/api/v1/documents/{doc_id}/content",
        headers=member["headers"],
        json={"content": "folder editor update"},
    )
    assert member_save.status_code == 200, member_save.text

    member_upload = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}&visibility=private",
        headers=member["headers"],
        files={"file": ("member-upload.md", b"member file", "text/markdown")},
    )
    assert member_upload.status_code == 201, member_upload.text
    assert member_upload.json()["folder_id"] == folder_id

    member_delete = await client.delete(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert member_delete.status_code == 403


@pytest.mark.asyncio
async def test_private_folder_hides_folder_and_child_documents_until_granted(client: AsyncClient):
    owner = await _register_owner(client, "private_folder_owner")
    entity_id = owner["entity_id"]
    member = await _create_entity_user(entity_id, "private_folder_member", "member")

    folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=owner["headers"],
        json={"name": "Private Folder"},
    )
    assert folder_resp.status_code == 201, folder_resp.text
    folder_id = folder_resp.json()["id"]

    upload_resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=owner["headers"],
        files={"file": ("private-folder-child.md", b"private folder content", "text/markdown")},
    )
    assert upload_resp.status_code == 201, upload_resp.text
    doc_id = upload_resp.json()["id"]
    assert upload_resp.json()["visibility"] == Visibility.ENTITY

    props_resp = await client.post(
        f"/api/v1/folders/{folder_id}/properties",
        headers=owner["headers"],
        json={"visibility": Visibility.PRIVATE, "cascade": False},
    )
    assert props_resp.status_code == 200, props_resp.text

    member_folders = await client.get("/api/v1/documents/folders", headers=member["headers"])
    assert member_folders.status_code == 200, member_folders.text
    assert folder_id not in {folder["id"] for folder in member_folders.json()}

    member_folder_docs = await client.get(
        f"/api/v1/documents?folder_id={folder_id}",
        headers=member["headers"],
    )
    assert member_folder_docs.status_code == 404

    member_all_docs = await client.get("/api/v1/documents", headers=member["headers"])
    assert member_all_docs.status_code == 200, member_all_docs.text
    assert member_all_docs.json()["items"] == []
    assert member_all_docs.json()["total_files"] == 0

    member_doc = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert member_doc.status_code == 404

    member_search = await client.get("/api/v1/search?q=private-folder-child", headers=member["headers"])
    assert member_search.status_code == 200, member_search.text
    assert member_search.json()["documents"] == []

    await _grant_folder_caps(
        entity_id,
        folder_id,
        member["id"],
        owner["user_id"],
        [Capability.VIEW],
    )

    granted_folders = await client.get("/api/v1/documents/folders", headers=member["headers"])
    assert granted_folders.status_code == 200, granted_folders.text
    assert folder_id in {folder["id"] for folder in granted_folders.json()}
    granted_folder = next(folder for folder in granted_folders.json() if folder["id"] == folder_id)
    assert granted_folder["document_count"] == 1

    granted_doc = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert granted_doc.status_code == 200, granted_doc.text

    granted_search = await client.get("/api/v1/search?q=private-folder-child", headers=member["headers"])
    assert granted_search.status_code == 200, granted_search.text
    assert {doc["id"] for doc in granted_search.json()["documents"]} == {doc_id}


@pytest.mark.asyncio
async def test_document_upload_and_folder_management_role_matrix(client: AsyncClient):
    owner = await _register_owner(client, "folder_perm_owner")
    entity_id = owner["entity_id"]
    member = await _create_entity_user(entity_id, "folder_perm_member", "member")
    viewer = await _create_entity_user(entity_id, "folder_perm_viewer", "viewer")

    owner_folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=owner["headers"],
        json={"name": "Owner Folder"},
    )
    assert owner_folder_resp.status_code == 201, owner_folder_resp.text
    owner_folder_id = owner_folder_resp.json()["id"]

    viewer_folder = await client.post(
        "/api/v1/documents/folders",
        headers=viewer["headers"],
        json={"name": "Viewer Folder"},
    )
    assert viewer_folder.status_code == 403

    viewer_upload = await client.post(
        "/api/v1/documents/upload",
        headers=viewer["headers"],
        files={"file": ("viewer.md", b"nope", "text/markdown")},
    )
    assert viewer_upload.status_code == 403

    member_folder_resp = await client.post(
        "/api/v1/documents/folders",
        headers=member["headers"],
        json={"name": "Member Folder"},
    )
    assert member_folder_resp.status_code == 201, member_folder_resp.text
    member_folder_id = member_folder_resp.json()["id"]

    member_rename_owner_folder = await client.put(
        f"/api/v1/documents/folders/{owner_folder_id}",
        headers=member["headers"],
        json={"name": "Renamed By Member"},
    )
    assert member_rename_owner_folder.status_code == 403

    member_upload_to_owner_folder = await client.post(
        f"/api/v1/documents/upload?folder_id={owner_folder_id}",
        headers=member["headers"],
        files={"file": ("member.md", b"blocked", "text/markdown")},
    )
    assert member_upload_to_owner_folder.status_code == 403

    member_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=member["id"],
        name="member-owned.md",
    )

    member_move_to_owner_folder = await client.post(
        f"/api/v1/documents/{member_doc_id}/move",
        headers=member["headers"],
        json={"folder_id": owner_folder_id},
    )
    assert member_move_to_owner_folder.status_code == 403

    member_move_to_member_folder = await client.post(
        f"/api/v1/documents/{member_doc_id}/move",
        headers=member["headers"],
        json={"folder_id": member_folder_id},
    )
    assert member_move_to_member_folder.status_code == 200, member_move_to_member_folder.text
    assert member_move_to_member_folder.json()["folder_id"] == member_folder_id


@pytest.mark.asyncio
async def test_runtime_document_tools_filter_by_user_and_workspace(client: AsyncClient):
    owner = await _register_owner(client, "runtime_owner")
    entity_id = owner["entity_id"]
    viewer = await _create_entity_user(entity_id, "runtime_viewer", "viewer")
    member = await _create_entity_user(entity_id, "runtime_member", "member")

    workspace_resp = await client.post(
        "/api/v1/workspaces",
        headers=owner["headers"],
        json={"name": "Runtime Workspace"},
    )
    workspace_id = workspace_resp.json()["id"]
    await _add_workspace_staff(workspace_id, viewer["id"], "viewer", owner["user_id"])

    entity_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="runtime entity reference",
    )
    await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="runtime private reference",
        visibility=Visibility.PRIVATE,
    )
    workspace_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="runtime workspace reference",
        visibility=Visibility.WORKSPACE,
        workspace_id=workspace_id,
    )

    viewer_workspace = json.loads(
        await runtime_list_documents_action(
            entity_id=entity_id,
            user_id=viewer["id"],
            workspace_id=workspace_id,
            params={"limit": 10},
        )
    )
    assert {doc["id"] for doc in viewer_workspace["documents"]} == {workspace_doc_id}

    member_workspace = json.loads(
        await runtime_list_documents_action(
            entity_id=entity_id,
            user_id=member["id"],
            workspace_id=workspace_id,
            params={"limit": 10},
        )
    )
    assert member_workspace["documents"] == []

    member_entity = json.loads(
        await runtime_list_documents_action(
            entity_id=entity_id,
            user_id=member["id"],
            params={"limit": 10},
        )
    )
    assert {doc["id"] for doc in member_entity["documents"]} == {entity_doc_id}

    rag_viewer = json.loads(
        await runtime_rag_action(
            entity_id=entity_id,
            user_id=viewer["id"],
            workspace_id=workspace_id,
            params={"question": "runtime workspace reference", "limit": 5},
        )
    )
    assert {source["document_id"] for source in rag_viewer["sources"]} == {workspace_doc_id}

    rag_member = json.loads(
        await runtime_rag_action(
            entity_id=entity_id,
            user_id=member["id"],
            workspace_id=workspace_id,
            params={"question": "runtime workspace reference", "limit": 5},
        )
    )
    assert rag_member["source_count"] == 0


@pytest.mark.asyncio
async def test_external_customer_rag_only_returns_client_visible_workspace_docs(client: AsyncClient):
    owner = await _register_owner(client, "rag_public_owner")
    entity_id = owner["entity_id"]

    workspace_resp = await client.post(
        "/api/v1/workspaces",
        headers=owner["headers"],
        json={"name": "Public RAG Workspace"},
    )
    assert workspace_resp.status_code == 201, workspace_resp.text
    workspace_id = workspace_resp.json()["id"]

    public_doc_id = await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="pricing policy customer visible",
        visibility=Visibility.WORKSPACE,
        workspace_id=workspace_id,
        client_visible=True,
    )
    await _seed_document(
        entity_id=entity_id,
        owner_id=owner["user_id"],
        name="pricing policy internal notes",
        visibility=Visibility.WORKSPACE,
        workspace_id=workspace_id,
        client_visible=False,
    )

    payload = json.loads(
        await runtime_rag_action(
            entity_id=entity_id,
            workspace_id=workspace_id,
            client_visible_only=True,
            params={"question": "pricing policy", "limit": 10},
        )
    )

    assert {source["document_id"] for source in payload["sources"]} == {public_doc_id}
    assert "internal notes" not in payload["context"]
