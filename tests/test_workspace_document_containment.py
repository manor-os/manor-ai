"""Regression: a workspace's knowledge net bounds the documents inside it.

The originally reported symptom: a non-member of a ``members_only`` workspace
got 404 on the workspace itself but could still read the documents in its
knowledge net — the workspace hid itself but not its contents. Documents carry
their own ``visibility``, which defaulted to ``entity``, and attaching one to a
workspace group never narrowed it.

Two rules are pinned here:

1. Container narrowing — an entity-visible document filed into a workspace is
   readable only by people who can read that workspace. Documents in no
   workspace, and workspaces that are ``entity_visible``, are unaffected.
2. Upload inheritance — a document inherits the visibility of the folder it
   lands in, instead of jumping to entity-wide the moment it is filed.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.document import (
    Document,
    DocumentFolder,
    DocumentGroup,
    DocumentGroupMember,
)
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.document_access import user_can_read_document
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _make_workspace(
    entity_id: str, name: str, owner_user_id: str, *, access_mode: str = "members_only",
) -> str:
    async with db_module.async_session() as db:
        ws = Workspace(
            entity_id=entity_id, name=name,
            settings={"access_mode": access_mode},
        )
        db.add(ws)
        await db.flush()
        ws_id = ws.id
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=owner_user_id,
            role="owner", status="active",
        ))
        await db.commit()
    return ws_id


async def _doc_in_workspace(
    entity_id: str, name: str, workspace_id: str | None, *, visibility: str = "entity",
) -> str:
    """A document, optionally filed into a workspace's knowledge net."""
    async with db_module.async_session() as db:
        doc = Document(entity_id=entity_id, name=name, visibility=visibility)
        db.add(doc)
        await db.flush()
        doc_id = doc.id
        if workspace_id:
            group = DocumentGroup(
                entity_id=entity_id, name=f"{name} group", workspace_id=workspace_id,
            )
            db.add(group)
            await db.flush()
            db.add(DocumentGroupMember(document_id=doc_id, group_id=group.id))
        await db.commit()
    return doc_id


async def _can_read(doc_id: str, entity_id: str, user_id: str, role: str) -> bool:
    async with db_module.async_session() as db:
        doc = await db.get(Document, doc_id)
        return await user_can_read_document(
            db, doc, entity_id=entity_id, user_id=user_id, role=role,
        )


@pytest.mark.asyncio
async def test_non_member_cannot_read_document_in_members_only_workspace(
    client: AsyncClient,
):
    """The reported leak."""
    owner_headers = await _auth(client, "wsdoc_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Client WS", owner["id"])
    doc_id = await _doc_in_workspace(owner["entity_id"], "quote.md", ws_id)

    outsider = await _create_entity_user(owner["entity_id"], "wsdoc_outsider", "member")

    assert not await _can_read(
        doc_id, owner["entity_id"], outsider["id"], "member"
    )
    r = await client.get(f"/api/v1/documents/{doc_id}", headers=outsider["headers"])
    assert r.status_code == 404, r.text

    # The workspace member reads it fine.
    assert await _can_read(doc_id, owner["entity_id"], owner["id"], "owner")


@pytest.mark.asyncio
async def test_document_outside_any_workspace_is_unaffected(client: AsyncClient):
    """No workspace, no narrowing — the common case must not regress."""
    owner_headers = await _auth(client, "wsdoc_loose_owner")
    owner = await _me(client, owner_headers)
    doc_id = await _doc_in_workspace(owner["entity_id"], "handbook.md", None)

    member = await _create_entity_user(owner["entity_id"], "wsdoc_loose_member", "member")
    assert await _can_read(doc_id, owner["entity_id"], member["id"], "member")

    r = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_entity_visible_workspace_does_not_narrow(client: AsyncClient):
    """An ``entity_visible`` workspace admits everyone, so nothing changes."""
    owner_headers = await _auth(client, "wsdoc_open_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(
        owner["entity_id"], "Open WS", owner["id"], access_mode="entity_visible",
    )
    doc_id = await _doc_in_workspace(owner["entity_id"], "notes.md", ws_id)

    member = await _create_entity_user(owner["entity_id"], "wsdoc_open_member", "member")
    assert await _can_read(doc_id, owner["entity_id"], member["id"], "member")


@pytest.mark.asyncio
async def test_second_workspace_never_removes_access(client: AsyncClient):
    """Readable through any one of its workspaces is enough."""
    owner_headers = await _auth(client, "wsdoc_multi_owner")
    owner = await _me(client, owner_headers)
    open_ws = await _make_workspace(
        owner["entity_id"], "Open", owner["id"], access_mode="entity_visible",
    )
    closed_ws = await _make_workspace(owner["entity_id"], "Closed", owner["id"])

    doc_id = await _doc_in_workspace(owner["entity_id"], "shared.md", open_ws)
    # Also file it into a restricted workspace.
    async with db_module.async_session() as db:
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="closed group", workspace_id=closed_ws,
        )
        db.add(group)
        await db.flush()
        db.add(DocumentGroupMember(document_id=doc_id, group_id=group.id))
        await db.commit()

    member = await _create_entity_user(owner["entity_id"], "wsdoc_multi_member", "member")
    assert await _can_read(doc_id, owner["entity_id"], member["id"], "member")


@pytest.mark.asyncio
async def test_entity_admin_without_global_workspace_permission_has_no_override(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "wsdoc_admin_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Locked", owner["id"])
    doc_id = await _doc_in_workspace(owner["entity_id"], "secret.md", ws_id)

    admin = await _create_entity_user(owner["entity_id"], "wsdoc_admin", "admin")
    assert not await _can_read(
        doc_id, owner["entity_id"], admin["id"], "admin"
    )


# ── Upload inheritance ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_document_inherits_folder_visibility(client: AsyncClient):
    """Filing a document must not make it more public than the folder."""
    from packages.core.services.document_service import create_document

    owner_headers = await _auth(client, "wsdoc_folder_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        folder = DocumentFolder(
            entity_id=owner["entity_id"], name="Private Folder", visibility="private",
        )
        db.add(folder)
        await db.flush()
        folder_id = folder.id
        await db.commit()

    async with db_module.async_session() as db:
        doc = await create_document(
            db,
            entity_id=owner["entity_id"],
            name="filed.md",
            folder_id=folder_id,
            owner_id=owner["id"],
        )
        await db.commit()
        assert doc.visibility == "private"


@pytest.mark.asyncio
async def test_root_document_still_defaults_private(client: AsyncClient):
    from packages.core.services.document_service import create_document

    owner_headers = await _auth(client, "wsdoc_root_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        doc = await create_document(
            db,
            entity_id=owner["entity_id"],
            name="loose.md",
            owner_id=owner["id"],
        )
        await db.commit()
        assert doc.visibility == "private"


@pytest.mark.asyncio
async def test_workspace_folder_creation_and_placement_share_with_workspace(client: AsyncClient):
    from apps.api.routers.documents import _enforce_folder_invariants
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
        ensure_workspace_document_folder,
    )

    headers = await _auth(client, "wsdoc_workspace_folder")
    owner = await _me(client, headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Shared files", owner["id"])
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        root = await ensure_workspace_artifact_folder(db, workspace)
        root_id = root.id
        await db.commit()

    created = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Reports", "parent_id": root_id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["visibility"] == "workspace"
    projected_folder_id = await ensure_workspace_document_folder(
        entity_id=owner["entity_id"],
        workspace_id=workspace_id,
        rel_path=f"Workspaces/_by_id/{root_id}/Reports/generated.md",
    )
    assert projected_folder_id == created.json()["id"]
    same_name = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Reports", "parent_id": root_id},
    )
    assert same_name.status_code == 409

    async with db_module.async_session() as db:
        visibility, _, _, adjustments = await _enforce_folder_invariants(
            db,
            entity_id=owner["entity_id"],
            folder_id=created.json()["id"],
            visibility="private",
            classification=None,
            client_visible=None,
        )
    assert visibility == "workspace"
    assert adjustments["visibility"]["reason"] == "workspace folder"

    narrowed_folder = await client.post(
        f"/api/v1/folders/{created.json()['id']}/properties",
        headers=headers,
        json={"visibility": "private"},
    )
    assert narrowed_folder.status_code == 409

    async with db_module.async_session() as db:
        doc = Document(
            entity_id=owner["entity_id"], name="team.md",
            owner_id=owner["id"], folder_id=created.json()["id"],
            visibility="workspace",
        )
        db.add(doc)
        await db.commit()
        doc_id = doc.id
    narrowed_document = await client.post(
        f"/api/v1/permissions/documents/{doc_id}/visibility",
        headers=headers,
        json={"visibility": "private"},
    )
    assert narrowed_document.status_code == 409

    member = await _create_entity_user(owner["entity_id"], "wsdoc_workspace_member", "member")
    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="member", status="active",
        ))
        await db.commit()
    shared = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
    assert shared.status_code == 200, shared.text


@pytest.mark.asyncio
async def test_workspace_contributor_can_upload_but_viewer_cannot(client: AsyncClient):
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    owner_headers = await _auth(client, "wsdoc_upload_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Contributor files", owner["id"])
    contributor = await _create_entity_user(owner["entity_id"], "wsdoc_contributor", "member")
    viewer = await _create_entity_user(owner["entity_id"], "wsdoc_viewer", "member")
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        root = await ensure_workspace_artifact_folder(db, workspace)
        root_id = root.id
        db.add_all([
            WorkspaceStaff(workspace_id=workspace_id, staff_id=None, user_id=contributor["id"], role="contributor", status="active"),
            WorkspaceStaff(workspace_id=workspace_id, staff_id=None, user_id=viewer["id"], role="viewer", status="active"),
        ])
        await db.commit()

    denied = await client.post(
        f"/api/v1/documents/upload?folder_id={root_id}",
        headers=viewer["headers"],
        files={"file": ("viewer.txt", b"denied", "text/plain")},
    )
    assert denied.status_code == 403
    created = await client.post(
        "/api/v1/documents/folders",
        headers=contributor["headers"],
        json={"name": "Shared notes", "parent_id": root_id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["visibility"] == "workspace"
    duplicate_from_owner = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": "Shared notes", "parent_id": root_id},
    )
    assert duplicate_from_owner.status_code == 409
    uploaded = await client.post(
        f"/api/v1/documents/upload?folder_id={root_id}&visibility=private",
        headers=contributor["headers"],
        files={"file": ("team.txt", b"workspace team", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["visibility"] == "workspace"
    readable = await client.get(
        f"/api/v1/documents/{uploaded.json()['id']}", headers=viewer["headers"],
    )
    assert readable.status_code == 200, readable.text


@pytest.mark.asyncio
async def test_workspace_group_upload_cannot_be_made_private(client: AsyncClient):
    owner_headers = await _auth(client, "wsdoc_chat_visibility_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Chat uploads", owner["id"])
    async with db_module.async_session() as db:
        doc = Document(
            entity_id=owner["entity_id"], name="chat.txt", owner_id=owner["id"],
            visibility="workspace", source="chat_upload",
        )
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Workspace uploads", workspace_id=workspace_id,
        )
        db.add_all([doc, group])
        await db.flush()
        db.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
        doc_id = doc.id
        await db.commit()

    narrowed = await client.post(
        f"/api/v1/permissions/documents/{doc_id}/visibility",
        headers=owner_headers,
        json={"visibility": "private"},
    )
    assert narrowed.status_code == 409


@pytest.mark.asyncio
async def test_moving_workspace_document_to_personal_root_revokes_workspace_read(client: AsyncClient):
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    owner_headers = await _auth(client, "wsdoc_move_out_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Move out", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_move_out_member", "member")
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        root = await ensure_workspace_artifact_folder(db, workspace)
        doc = Document(
            entity_id=owner["entity_id"], name="move-out.md", owner_id=owner["id"],
            folder_id=root.id, visibility="workspace",
            metadata_={"origin": {"workspace_id": workspace_id}},
        )
        db.add(doc)
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        doc_id = doc.id

    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])).status_code == 200
    workspace_files = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents", headers=member["headers"],
    )
    assert workspace_files.status_code == 200, workspace_files.text
    assert doc_id in {
        document["id"] for group in workspace_files.json()
        for document in group["documents"]
    }
    moved = await client.post(
        f"/api/v1/documents/{doc_id}/move", headers=owner_headers,
        json={"folder_id": None},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["visibility"] == "private"
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])).status_code == 404
    async with db_module.async_session() as db:
        saved = await db.get(Document, doc_id)
        assert "workspace_id" not in (saved.metadata_ or {}).get("origin", {})
        workspace = await db.get(Workspace, workspace_id)
        from datetime import datetime, timezone

        workspace.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=owner_headers)).status_code == 200


@pytest.mark.asyncio
async def test_moving_unfiled_workspace_artifact_to_root_revokes_workspace_read(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from packages.core.config import get_settings
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    headers = await _auth(client, "wsdoc_path_move_owner")
    owner = await _me(client, headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Path move", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_path_move_member", "member")
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        root = await ensure_workspace_artifact_folder(db, workspace)
        rel_path = f"Workspaces/_by_id/{root.id}/from-path.md"
        doc = Document(
            entity_id=owner["entity_id"], name="from-path.md", owner_id=owner["id"],
            fs_path=rel_path, visibility="workspace",
            metadata_={"origin": {"workspace_id": workspace_id}},
        )
        db.add(doc)
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        doc_id = doc.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    source = tmp_path / owner["entity_id"] / rel_path
    source.parent.mkdir(parents=True)
    source.write_text("private after move")
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])).status_code == 200
    moved = await client.post(
        f"/api/v1/documents/{doc_id}/move", headers=headers,
        json={"folder_id": None},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["visibility"] == "private"
    assert moved.json()["fs_path"] == "from-path.md"
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])).status_code == 404


@pytest.mark.asyncio
async def test_deleted_workspace_does_not_hide_personal_or_entity_context_files(
    client: AsyncClient,
):
    from datetime import datetime, timezone

    from packages.core.services.document_access import list_visible_documents

    owner_headers = await _auth(client, "wsdoc_context_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Context only", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_context_member", "member")
    async with db_module.async_session() as db:
        personal = Document(
            entity_id=owner["entity_id"], name="personal-generated.md",
            owner_id=owner["id"], visibility="private", source="ai_generated",
            metadata_={"content": "personal text", "origin": {"workspace_id": workspace_id}},
        )
        historical = Document(
            entity_id=owner["entity_id"], name="historical-entity.md",
            owner_id=owner["id"], visibility="entity",
            metadata_={"content": "entity text", "origin": {"workspace_id": workspace_id}},
        )
        db.add_all([personal, historical])
        await db.flush()
        personal_id, historical_id = personal.id, historical.id
        await db.commit()
    workspace_files = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents", headers=owner_headers,
    )
    assert workspace_files.status_code == 200, workspace_files.text
    projected_ids = {
        document["id"] for group in workspace_files.json()
        for document in group["documents"]
    }
    assert personal_id not in projected_ids
    assert historical_id not in projected_ids
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        workspace.deleted_at = datetime.now(timezone.utc)
        await db.commit()

    checks = await asyncio.gather(*[
        client.get(f"/api/v1/documents/{doc_id}/content", headers=headers)
        for _ in range(12)
        for doc_id, headers in (
            (personal_id, owner_headers),
            (historical_id, member["headers"]),
            (personal_id, member["headers"]),
        )
    ])
    assert [response.status_code for response in checks] == [200, 200, 404] * 12
    async with db_module.async_session() as db:
        owner_docs, _ = await list_visible_documents(
            db, owner["entity_id"], user_id=owner["id"], role="owner", limit=None,
        )
        member_docs, _ = await list_visible_documents(
            db, owner["entity_id"], user_id=member["id"], role="member", limit=None,
        )
    assert {personal_id, historical_id}.issubset({doc.id for doc in owner_docs})
    assert historical_id in {doc.id for doc in member_docs}
    assert personal_id not in {doc.id for doc in member_docs}


@pytest.mark.asyncio
async def test_personal_generated_origin_does_not_become_second_workspace_share(
    client: AsyncClient,
):
    from datetime import datetime, timezone

    owner_headers = await _auth(client, "wsdoc_origin_share_owner")
    owner = await _me(client, owner_headers)
    shared_workspace_id = await _make_workspace(owner["entity_id"], "Explicit audience", owner["id"])
    context_workspace_id = await _make_workspace(owner["entity_id"], "Generation context", owner["id"])
    shared_member = await _create_entity_user(owner["entity_id"], "wsdoc_origin_shared_member", "member")
    context_member = await _create_entity_user(owner["entity_id"], "wsdoc_origin_context_member", "member")
    async with db_module.async_session() as db:
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Explicit share",
            workspace_id=shared_workspace_id,
        )
        doc = Document(
            entity_id=owner["entity_id"], name="generated.md", owner_id=owner["id"],
            visibility="private", source="ai_generated",
            metadata_={"content": "owner's generated work", "origin": {"workspace_id": context_workspace_id}},
        )
        db.add_all([group, doc])
        await db.flush()
        db.add_all([
            WorkspaceStaff(workspace_id=shared_workspace_id, staff_id=None,
                           user_id=shared_member["id"], role="viewer", status="active"),
            WorkspaceStaff(workspace_id=context_workspace_id, staff_id=None,
                           user_id=context_member["id"], role="viewer", status="active"),
        ])
        await db.commit()
        doc_id, group_id = doc.id, group.id

    added = await client.post(
        f"/api/v1/documents/{doc_id}/groups/{group_id}", headers=owner_headers,
    )
    assert added.status_code == 200, added.text
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=shared_member["headers"])).status_code == 200
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=context_member["headers"])).status_code == 404
    scoped_list = await client.get(
        "/api/v1/documents", headers=owner_headers,
        params={"workspace_id": context_workspace_id},
    )
    assert scoped_list.status_code == 200, scoped_list.text
    assert doc_id not in {item["id"] for item in scoped_list.json()["items"]}
    context_snapshot = await client.get(
        f"/api/v1/workspaces/{context_workspace_id}/documents", headers=owner_headers,
    )
    assert context_snapshot.status_code == 200, context_snapshot.text
    assert doc_id not in {
        document["id"] for group in context_snapshot.json()
        for document in group["documents"]
    }
    async with db_module.async_session() as db:
        context_workspace = await db.get(Workspace, context_workspace_id)
        context_workspace.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=shared_member["headers"])).status_code == 200


@pytest.mark.asyncio
async def test_concurrent_workspace_chat_moves_revoke_old_group_shares(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from packages.core.config import get_settings

    owner_headers = await _auth(client, "wsdoc_concurrent_move_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Chat move stress", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_concurrent_move_member", "member")
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    doc_ids = []
    async with db_module.async_session() as db:
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Chat uploads", workspace_id=workspace_id,
        )
        db.add(group)
        await db.flush()
        for index in range(24):
            rel_path = f"uploads/chat/chat-{index}.txt"
            doc = Document(
                entity_id=owner["entity_id"], name=f"chat-{index}.txt",
                owner_id=owner["id"], source="chat_upload", fs_path=rel_path,
                visibility="workspace",
                metadata_={"origin": {"workspace_id": workspace_id}},
            )
            db.add(doc)
            await db.flush()
            db.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
            doc_ids.append(doc.id)
            source = tmp_path / owner["entity_id"] / rel_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"chat payload {index}")
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()

    before = await client.get(f"/api/v1/documents/{doc_ids[0]}", headers=member["headers"])
    assert before.status_code == 200, before.text
    moves = await asyncio.gather(*[
        client.post(
            f"/api/v1/documents/{doc_id}/move", headers=owner_headers,
            json={"folder_id": None},
        )
        for doc_id in doc_ids
    ])
    assert all(response.status_code == 200 for response in moves), [
        (response.status_code, response.text) for response in moves if response.status_code != 200
    ]
    assert all(response.json()["visibility"] == "private" for response in moves)
    reads = await asyncio.gather(*[
        client.get(f"/api/v1/documents/{doc_id}/content", headers=headers)
        for doc_id in doc_ids
        for headers in (owner_headers, member["headers"])
    ])
    assert [response.status_code for response in reads] == [200, 404] * len(doc_ids)
    async with db_module.async_session() as db:
        from sqlalchemy import select

        stale_links = (await db.execute(
            select(DocumentGroupMember.document_id).where(
                DocumentGroupMember.document_id.in_(doc_ids),
            )
        )).scalars().all()
        assert stale_links == []
        for doc_id in doc_ids:
            saved = await db.get(Document, doc_id)
            assert "workspace_id" not in (saved.metadata_ or {}).get("origin", {})


@pytest.mark.asyncio
async def test_two_moves_of_same_chat_upload_cannot_leave_stale_share(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings

    owner_headers = await _auth(client, "wsdoc_racing_move_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Racing moves", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_racing_move_member", "member")
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    async with db_module.async_session() as db:
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Racing chat uploads",
            workspace_id=workspace_id,
        )
        doc = Document(
            entity_id=owner["entity_id"], name="race.txt", owner_id=owner["id"],
            source="chat_upload", fs_path="uploads/chat/race.txt", visibility="workspace",
            metadata_={"origin": {"workspace_id": workspace_id}},
        )
        db.add_all([group, doc])
        await db.flush()
        db.add_all([
            DocumentGroupMember(document_id=doc.id, group_id=group.id),
            WorkspaceStaff(workspace_id=workspace_id, staff_id=None,
                           user_id=member["id"], role="viewer", status="active"),
        ])
        await db.commit()
        doc_id = doc.id
    source = tmp_path / owner["entity_id"] / "uploads/chat/race.txt"
    source.parent.mkdir(parents=True)
    source.write_text("race payload")

    original_get_visible = documents_router.get_visible_document
    both_loaded = asyncio.Event()
    arrivals = 0

    async def synchronize_reads(*args, **kwargs):
        nonlocal arrivals
        visible = await original_get_visible(*args, **kwargs)
        arrivals += 1
        if arrivals == 2:
            both_loaded.set()
        await asyncio.wait_for(both_loaded.wait(), timeout=10)
        return visible

    monkeypatch.setattr(documents_router, "get_visible_document", synchronize_reads)
    results = await asyncio.gather(*[
        client.post(
            f"/api/v1/documents/{doc_id}/move", headers=owner_headers,
            json={"folder_id": None},
        )
        for _ in range(2)
    ])
    assert sorted(response.status_code for response in results) == [200, 409], [
        (response.status_code, response.text) for response in results
    ]
    monkeypatch.setattr(documents_router, "get_visible_document", original_get_visible)
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=owner_headers)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=member["headers"])).status_code == 404
    async with db_module.async_session() as db:
        assert (await db.get(DocumentGroupMember, (doc_id, group.id))) is None


@pytest.mark.asyncio
async def test_moving_chat_upload_between_workspaces_drops_old_audience(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from packages.core.config import get_settings
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    owner_headers = await _auth(client, "wsdoc_cross_move_owner")
    owner = await _me(client, owner_headers)
    source_workspace_id = await _make_workspace(owner["entity_id"], "Source chat", owner["id"])
    target_workspace_id = await _make_workspace(owner["entity_id"], "Target files", owner["id"])
    old_member = await _create_entity_user(owner["entity_id"], "wsdoc_cross_old", "member")
    new_member = await _create_entity_user(owner["entity_id"], "wsdoc_cross_new", "member")
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    async with db_module.async_session() as db:
        target_workspace = await db.get(Workspace, target_workspace_id)
        target_root = await ensure_workspace_artifact_folder(db, target_workspace)
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Source uploads",
            workspace_id=source_workspace_id,
        )
        doc = Document(
            entity_id=owner["entity_id"], name="cross.txt", owner_id=owner["id"],
            source="chat_upload", fs_path="uploads/chat/cross.txt", visibility="workspace",
            metadata_={"origin": {"workspace_id": source_workspace_id}},
        )
        db.add_all([group, doc])
        await db.flush()
        db.add_all([
            DocumentGroupMember(document_id=doc.id, group_id=group.id),
            WorkspaceStaff(workspace_id=source_workspace_id, staff_id=None,
                           user_id=old_member["id"], role="viewer", status="active"),
            WorkspaceStaff(workspace_id=target_workspace_id, staff_id=None,
                           user_id=new_member["id"], role="viewer", status="active"),
        ])
        await db.commit()
        doc_id, target_folder_id = doc.id, target_root.id
    source = tmp_path / owner["entity_id"] / "uploads/chat/cross.txt"
    source.parent.mkdir(parents=True)
    source.write_text("cross-workspace payload")

    moved = await client.post(
        f"/api/v1/documents/{doc_id}/move", headers=owner_headers,
        json={"folder_id": target_folder_id},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["visibility"] == "workspace"
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=old_member["headers"])).status_code == 404
    assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=new_member["headers"])).status_code == 200
    async with db_module.async_session() as db:
        saved = await db.get(Document, doc_id)
        assert saved.metadata_["origin"]["workspace_id"] == target_workspace_id
        assert (await db.get(DocumentGroupMember, (doc_id, group.id))) is None


@pytest.mark.asyncio
async def test_private_file_with_stale_group_link_does_not_publish_derived_media(
    client: AsyncClient,
):
    from apps.api.routers.media import _workspace_output_destination
    from packages.core.models.user import User

    owner_headers = await _auth(client, "wsdoc_private_media_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Old media context", owner["id"])
    async with db_module.async_session() as db:
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Old files", workspace_id=workspace_id,
        )
        doc = Document(
            entity_id=owner["entity_id"], name="private.mp4", owner_id=owner["id"],
            visibility="private", fs_path="private.mp4", mime_type="video/mp4",
            metadata_={"origin": {"workspace_id": workspace_id}},
        )
        db.add_all([group, doc])
        await db.flush()
        db.add(DocumentGroupMember(document_id=doc.id, group_id=group.id))
        await db.commit()
        actor = await db.get(User, owner["id"])
        assert await _workspace_output_destination(db, doc, actor) == (None, None, [])


@pytest.mark.asyncio
async def test_adding_private_document_to_workspace_group_shares_it(client: AsyncClient):
    owner_headers = await _auth(client, "wsdoc_group_share_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Knowledge group share", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_group_share_member", "member")
    async with db_module.async_session() as db:
        folder = DocumentFolder(
            entity_id=owner["entity_id"], name="Personal sources",
            owner_id=owner["id"], visibility="private",
        )
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Workspace sources",
            workspace_id=workspace_id,
        )
        db.add_all([folder, group])
        await db.flush()
        docs = [Document(
            entity_id=owner["entity_id"], name=f"personal-{index}.md",
            owner_id=owner["id"], folder_id=folder.id, visibility="private",
            metadata_={"content": f"shared content {index}"},
        ) for index in (1, 2)]
        db.add_all(docs)
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        group_id, doc_ids = group.id, [doc.id for doc in docs]

    single = await client.post(
        f"/api/v1/documents/{doc_ids[0]}/groups/{group_id}", headers=owner_headers,
    )
    assert single.status_code == 200, single.text
    batch = await client.post(
        "/api/v1/documents/groups/batch-add", headers=owner_headers,
        json={"document_ids": [doc_ids[1]], "group_id": group_id},
    )
    assert batch.status_code == 200, batch.text
    for doc_id in doc_ids:
        readable = await client.get(f"/api/v1/documents/{doc_id}", headers=member["headers"])
        assert readable.status_code == 200, readable.text
        content = await client.get(f"/api/v1/documents/{doc_id}/content", headers=member["headers"])
        assert content.status_code == 200, content.text
    workspace_documents = await client.get(
        "/api/v1/documents", headers=member["headers"],
        params={"workspace_id": workspace_id},
    )
    assert workspace_documents.status_code == 200, workspace_documents.text
    assert set(doc_ids).issubset({item["id"] for item in workspace_documents.json()["items"]})
    member_folders = await client.get("/api/v1/documents/folders", headers=member["headers"])
    assert folder.id not in {item["id"] for item in member_folders.json()}
    async with db_module.async_session() as db:
        assert (await db.get(DocumentFolder, folder.id)).visibility == "private"


@pytest.mark.asyncio
async def test_legacy_workspace_video_export_stays_shared(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from apps.api.routers import media
    from packages.core.config import get_settings
    from packages.core.services.video_editor_render import VideoEditorRenderResult
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    owner_headers = await _auth(client, "wsdoc_legacy_video_owner")
    owner = await _me(client, owner_headers)
    workspace_name = "Legacy video exports"
    workspace_id = await _make_workspace(owner["entity_id"], workspace_name, owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_legacy_video_member", "member")
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        root = await ensure_workspace_artifact_folder(db, workspace)
        source = Document(
            entity_id=owner["entity_id"], name="source.mp4", owner_id=owner["id"],
            folder_id=root.id, fs_path=f"Workspaces/{workspace_name}/source.mp4",
            visibility="workspace", mime_type="video/mp4", file_type="mp4",
        )
        db.add(source)
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        source_id = source.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))

    async def fake_finalize(_preview_path, output_path, **_kwargs):
        with open(output_path, "wb") as output:
            output.write(b"final")
        return VideoEditorRenderResult(
            duration_seconds=1, width=1, height=1, fps=30, file_size=5,
            has_audio=False, video_codec="h264", audio_codec=None,
        )

    monkeypatch.setattr(media, "finalize_video_editor_preview", fake_finalize)
    exported = await client.post(
        "/api/v1/media/video-editor/finalize",
        headers=owner_headers,
        data={"source_document_id": source_id, "output_name": "edited.mp4"},
        files={"preview": ("preview.webm", b"preview", "video/webm")},
    )
    assert exported.status_code == 200, exported.text
    document = exported.json()["document"]
    assert document["visibility"] == "workspace"
    assert document["folder_id"] == root.id
    assert (await client.get(
        f"/api/v1/documents/{document['id']}", headers=member["headers"],
    )).status_code == 200


@pytest.mark.asyncio
async def test_workspace_chat_video_export_is_shared(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from apps.api.routers import media
    from packages.core.config import get_settings
    from packages.core.services.video_editor_render import VideoEditorRenderResult

    owner_headers = await _auth(client, "wsdoc_chat_video_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Chat video", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_chat_video_member", "member")
    async with db_module.async_session() as db:
        source = Document(
            entity_id=owner["entity_id"], name="source.mp4", owner_id=owner["id"],
            fs_path="uploads/chat/source.mp4", visibility="workspace",
            source="chat_upload", mime_type="video/mp4", file_type="mp4",
        )
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Chat files", workspace_id=workspace_id,
        )
        db.add_all([source, group])
        await db.flush()
        db.add(DocumentGroupMember(document_id=source.id, group_id=group.id))
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        source_id = source.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))

    async def fake_finalize(_preview_path, output_path, **_kwargs):
        with open(output_path, "wb") as output:
            output.write(b"final")
        return VideoEditorRenderResult(
            duration_seconds=1, width=1, height=1, fps=30, file_size=5,
            has_audio=False, video_codec="h264", audio_codec=None,
        )

    monkeypatch.setattr(media, "finalize_video_editor_preview", fake_finalize)
    exported = await client.post(
        "/api/v1/media/video-editor/finalize", headers=owner_headers,
        data={"source_document_id": source_id, "output_name": "edited.mp4"},
        files={"preview": ("preview.webm", b"preview", "video/webm")},
    )
    assert exported.status_code == 200, exported.text
    document = exported.json()["document"]
    assert document["visibility"] == "workspace"
    assert document["fs_path"].startswith("Workspaces/_by_id/")
    assert (await client.get(
        f"/api/v1/documents/{document['id']}", headers=member["headers"],
    )).status_code == 200


@pytest.mark.asyncio
async def test_personal_chat_video_export_lands_in_private_knowledge(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from apps.api.routers import media
    from packages.core.config import get_settings
    from packages.core.services.video_editor_render import VideoEditorRenderResult

    headers = await _auth(client, "wsdoc_personal_video_owner")
    owner = await _me(client, headers)
    other = await _create_entity_user(owner["entity_id"], "wsdoc_personal_video_other", "member")
    async with db_module.async_session() as db:
        source = Document(
            entity_id=owner["entity_id"], name="source.mp4", owner_id=owner["id"],
            fs_path="uploads/chat/source.mp4", visibility="private",
            source="chat_upload", mime_type="video/mp4", file_type="mp4",
        )
        db.add(source)
        await db.commit()
        source_id = source.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))

    async def fake_finalize(_preview_path, output_path, **_kwargs):
        with open(output_path, "wb") as output:
            output.write(b"final")
        return VideoEditorRenderResult(
            duration_seconds=1, width=1, height=1, fps=30, file_size=5,
            has_audio=False, video_codec="h264", audio_codec=None,
        )

    monkeypatch.setattr(media, "finalize_video_editor_preview", fake_finalize)
    exported = await client.post(
        "/api/v1/media/video-editor/finalize", headers=headers,
        data={"source_document_id": source_id, "output_name": "edited.mp4"},
        files={"preview": ("preview.webm", b"preview", "video/webm")},
    )
    assert exported.status_code == 200, exported.text
    document = exported.json()["document"]
    assert document["visibility"] == "private"
    assert document["folder_id"] is None
    assert "/" not in document["fs_path"]
    assert (await client.get(
        f"/api/v1/documents/{document['id']}", headers=headers,
    )).status_code == 200
    assert (await client.get(
        f"/api/v1/documents/{document['id']}", headers=other["headers"],
    )).status_code == 404


@pytest.mark.asyncio
async def test_workspace_chat_free_music_import_is_shared(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from apps.api.routers import media
    from packages.core.config import get_settings
    from packages.core.services.open_music_catalog import DownloadedOpenMusic, OpenMusicTrack

    headers = await _auth(client, "wsdoc_chat_music_owner")
    owner = await _me(client, headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Chat music", owner["id"])
    member = await _create_entity_user(owner["entity_id"], "wsdoc_chat_music_member", "member")
    async with db_module.async_session() as db:
        source = Document(
            entity_id=owner["entity_id"], name="source.mp4", owner_id=owner["id"],
            fs_path="uploads/chat/source.mp4", visibility="workspace",
            source="chat_upload", mime_type="video/mp4", file_type="mp4",
        )
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Chat files", workspace_id=workspace_id,
        )
        db.add_all([source, group])
        await db.flush()
        db.add(DocumentGroupMember(document_id=source.id, group_id=group.id))
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=member["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        source_id = source.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    track = OpenMusicTrack(
        id="test-track", title="Music", creator="Creator", duration_seconds=1,
        preview_url="https://example.test/preview", source_url="https://example.test/source",
        license="cc0", license_name="CC0", license_url="https://example.test/license",
        attribution="Creator", source="test", provider="test", genres=(),
    )

    async def fake_track(_track_id):
        return track

    async def fake_download(_track, target_path, **_kwargs):
        with open(target_path, "wb") as output:
            output.write(b"music")
        return DownloadedOpenMusic(file_size=5, mime_type="audio/mpeg", extension="mp3")

    monkeypatch.setattr(media, "get_open_music_track", fake_track)
    monkeypatch.setattr(media, "download_open_music_track", fake_download)
    imported = await client.post(
        "/api/v1/media/free-music/import", headers=headers,
        json={"track_id": track.id, "source_document_id": source_id},
    )
    assert imported.status_code == 201, imported.text
    document = imported.json()["document"]
    assert document["visibility"] == "workspace"
    assert document["fs_path"].startswith("Workspaces/_by_id/")
    assert (await client.get(
        f"/api/v1/documents/{document['id']}", headers=member["headers"],
    )).status_code == 200


@pytest.mark.asyncio
async def test_workspace_viewer_cannot_publish_derived_video(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    from packages.core.config import get_settings

    owner_headers = await _auth(client, "wsdoc_media_gate_owner")
    owner = await _me(client, owner_headers)
    workspace_id = await _make_workspace(owner["entity_id"], "Media gate", owner["id"])
    viewer = await _create_entity_user(owner["entity_id"], "wsdoc_media_gate_viewer", "member")
    async with db_module.async_session() as db:
        source = Document(
            entity_id=owner["entity_id"], name="source.mp4", owner_id=viewer["id"],
            fs_path="uploads/chat/source.mp4", visibility="workspace",
            source="chat_upload", mime_type="video/mp4", file_type="mp4",
        )
        group = DocumentGroup(
            entity_id=owner["entity_id"], name="Chat files", workspace_id=workspace_id,
        )
        db.add_all([source, group])
        await db.flush()
        db.add(DocumentGroupMember(document_id=source.id, group_id=group.id))
        db.add(WorkspaceStaff(
            workspace_id=workspace_id, staff_id=None, user_id=viewer["id"],
            role="viewer", status="active",
        ))
        await db.commit()
        source_id = source.id

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    rejected = await client.post(
        "/api/v1/media/video-editor/finalize", headers=viewer["headers"],
        data={"source_document_id": source_id, "output_name": "edited.mp4"},
        files={"preview": ("preview.webm", b"preview", "video/webm")},
    )
    assert rejected.status_code == 403, rejected.text


@pytest.mark.asyncio
async def test_explicit_visibility_always_wins(client: AsyncClient):
    from packages.core.services.document_service import create_document

    owner_headers = await _auth(client, "wsdoc_explicit_owner")
    owner = await _me(client, owner_headers)

    async with db_module.async_session() as db:
        folder = DocumentFolder(
            entity_id=owner["entity_id"], name="Private Folder 2", visibility="private",
        )
        db.add(folder)
        await db.flush()
        folder_id = folder.id
        await db.commit()

    async with db_module.async_session() as db:
        doc = await create_document(
            db,
            entity_id=owner["entity_id"],
            name="explicit.md",
            folder_id=folder_id,
            owner_id=owner["id"],
            visibility="entity",
        )
        await db.commit()
        assert doc.visibility == "entity"
