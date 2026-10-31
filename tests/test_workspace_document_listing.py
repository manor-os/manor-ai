"""Workspace file snapshots keep ACLs and bounded database round trips."""

from datetime import UTC, datetime
from time import perf_counter
from types import SimpleNamespace

import pytest
from sqlalchemy import event

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder, DocumentGroup, DocumentGroupMember
from packages.core.models.permission import Capability, ResourceGrant, ResourceType, SubjectType
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.user import User
from tests.test_document_permissions import _auth, _create_entity_user


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "member"])
async def test_workspace_file_query_count_does_not_grow_per_document_or_group(
    client, db_session, role,
):
    headers = await _auth(client, f"file_snapshot_{role}")
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "File snapshot"},
    )
    assert response.status_code == 201, response.text
    workspace = response.json()
    workspace_id, entity_id = workspace["id"], workspace["entity_id"]
    if role == "member":
        member = await _create_entity_user(entity_id, "file_snapshot_reader")
        headers = member["headers"]
        db_session.add(WorkspaceStaff(
            workspace_id=workspace_id, user_id=member["id"], role="member", status="active",
        ))

    async def add_files(start, stop):
        for index in range(start, stop):
            group_id, folder_id, document_id = (generate_ulid() for _ in range(3))
            db_session.add_all([
                DocumentGroup(
                    id=group_id, entity_id=entity_id, workspace_id=workspace_id,
                    name=f"Group {index}",
                ),
                DocumentFolder(
                    id=folder_id, entity_id=entity_id, name=f"Folder {index}",
                    parent_id=workspace["artifact_folder_id"], visibility="workspace",
                ),
                Document(
                    id=document_id, entity_id=entity_id, name=f"file-{index}.md",
                    file_type="md", folder_id=folder_id, visibility="workspace",
                    metadata_={"origin": {"workspace_id": workspace_id}},
                ),
                DocumentGroupMember(group_id=group_id, document_id=document_id),
            ])
        await db_session.commit()

    url = f"/api/v1/workspaces/{workspace_id}/documents"
    await add_files(0, 1)
    # Complete the legacy default-collection initialization before measuring reads.
    assert (await client.get(url, headers=headers)).status_code == 200

    async def snapshot():
        statements = []

        def count_query(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db_module.engine.sync_engine, "before_cursor_execute", count_query)
        started = perf_counter()
        try:
            result = await client.get(url, headers=headers)
        finally:
            event.remove(db_module.engine.sync_engine, "before_cursor_execute", count_query)
        elapsed = perf_counter() - started
        assert result.status_code == 200, result.text
        assert not any(sql.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for sql in statements)
        return result.json(), len(statements), elapsed

    small, small_count, small_seconds = await snapshot()
    await add_files(1, 25)
    large, large_count, large_seconds = await snapshot()
    assert sum(group["document_count"] for group in small) == 1
    assert sum(group["document_count"] for group in large) == 25
    assert all(
        document["folder_id"] in {folder["id"] for folder in group["folders"]}
        for group in large for document in group["documents"]
    )
    print(
        f"\n{role}: 1 file/group = {small_count} SQL / {small_seconds:.3f}s; "
        f"25 files/groups = {large_count} SQL / {large_seconds:.3f}s"
    )
    assert large_count <= small_count + 4


@pytest.mark.asyncio
@pytest.mark.parametrize("initialize", [True, False])
async def test_workspace_file_snapshot_preserves_acl_deduplication_and_lifecycle(client, db_session, initialize):
    owner_headers = await _auth(client, "file_snapshot_acl")
    response = await client.post(
        "/api/v1/workspaces", headers=owner_headers, json={"name": "File ACL"},
    )
    assert response.status_code == 201, response.text
    workspace = response.json()
    workspace_id, entity_id = workspace["id"], workspace["entity_id"]
    member = await _create_entity_user(entity_id, "file_snapshot_acl_reader")
    group_id, second_group_id, hidden_folder_id, deleted_workspace_id = (
        generate_ulid() for _ in range(4)
    )
    db_session.add_all([
        WorkspaceStaff(workspace_id=workspace_id, user_id=member["id"], role="member", status="active"),
        Workspace(id=deleted_workspace_id, entity_id=entity_id, name="Deleted", deleted_at=datetime.now(UTC)),
        DocumentGroup(id=group_id, entity_id=entity_id, workspace_id=workspace_id, name="First"),
        DocumentGroup(id=second_group_id, entity_id=entity_id, workspace_id=workspace_id, name="Second"),
        DocumentFolder(id=hidden_folder_id, entity_id=entity_id, name="Private folder", visibility="private"),
    ])

    def document(name, **kwargs):
        return Document(
            id=generate_ulid(), entity_id=entity_id, name=name, file_type="md",
            metadata_={"origin": {"workspace_id": workspace_id}}, **kwargs,
        )

    visible = document("visible.md", visibility="workspace")
    private = document("private.md", visibility="private")
    shared = document("shared.md", visibility="private", folder_id=hidden_folder_id)
    quarantined = document("quarantined.md", quarantine_status="quarantined")
    deleted = document("deleted.md", visibility="workspace")
    # Knowledge Documents are Entity-owned. Its historical source Workspace
    # may be deleted without removing a current Workspace group membership.
    deleted.metadata_ = {"origin": {"workspace_id": deleted_workspace_id}}
    artifact = document("artifact.md", visibility="workspace")
    grouped = [visible, private, shared, quarantined, deleted]
    db_session.add_all([*grouped, artifact])
    db_session.add_all([
        DocumentGroupMember(group_id=group_id, document_id=doc.id) for doc in grouped
    ])
    db_session.add(DocumentGroupMember(group_id=second_group_id, document_id=shared.id))
    db_session.add(ResourceGrant(
        entity_id=entity_id, resource_type=ResourceType.DOCUMENT, resource_id=shared.id,
        subject_type=SubjectType.USER, subject_id=member["id"], capabilities=[Capability.VIEW],
        granted_at=datetime.now(UTC),
    ))
    await db_session.commit()

    url = f"/api/v1/workspaces/{workspace_id}/documents?initialize={str(initialize).lower()}"
    for headers, expected_ids in [
        (member["headers"], {visible.id, shared.id, deleted.id}),
        (owner_headers, {visible.id, private.id, shared.id, deleted.id}),
    ]:
        result = await client.get(url, headers=headers)
        assert result.status_code == 200, result.text
        groups = {group["id"]: group for group in result.json()}
        assert {doc["id"] for doc in groups[group_id]["documents"]} == expected_ids
        assert groups[group_id]["document_count"] == len(expected_ids)
        assert {doc["id"] for doc in groups[second_group_id]["documents"]} == {shared.id}
        artifacts = next(group for group in result.json() if group["is_workspace_file_bucket"])
        assert {doc["id"] for doc in artifacts["documents"]} == {artifact.id}
        if headers == member["headers"]:
            assert groups[group_id]["folders"] == []
            assert next(doc for doc in groups[group_id]["documents"] if doc["id"] == shared.id)["folder_id"] is None

    outsider = await _create_entity_user(entity_id, "file_snapshot_outsider")
    assert (await client.get(url, headers=outsider["headers"])).status_code == 404
    ws = await db_session.get(Workspace, workspace_id)
    ws.status = "paused"
    await db_session.commit()
    assert (await client.get(url, headers=member["headers"])).status_code == 200
    ws.deleted_at = datetime.now(UTC)
    await db_session.commit()
    assert (await client.get(url, headers=owner_headers)).status_code == 404


@pytest.mark.asyncio
async def test_workspace_staff_roles_govern_linked_file_access(client, db_session):
    from apps.api.routers.documents import _can_delete_document, _can_manage_document
    from packages.core.services.document_access import (
        DocumentAccessContext,
        effective_document_capabilities_for_user,
        user_can_edit_document,
        user_can_read_document,
        user_can_share_document_externally,
        user_owns_workspace_folder,
        workspace_folder_capabilities_for_user,
    )

    headers = await _auth(client, "workspace_file_owner_entity")
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Owned files"},
    )
    assert response.status_code == 201, response.text
    workspace = response.json()
    entity_id, workspace_id = workspace["entity_id"], workspace["id"]
    owner = await _create_entity_user(entity_id, "workspace_file_staff_owner")
    creator = await _create_entity_user(entity_id, "workspace_file_creator")
    recipient = await _create_entity_user(entity_id, "workspace_file_recipient")
    owner_membership = WorkspaceStaff(
        workspace_id=workspace_id, user_id=owner["id"], role="owner", status="active",
    )
    personal_folder = DocumentFolder(
        entity_id=entity_id, name="Creator private", owner_id=creator["id"],
        visibility="private",
    )
    workspace_folder = DocumentFolder(
        entity_id=entity_id, name="Team files", owner_id=creator["id"],
        parent_id=workspace["artifact_folder_id"], visibility="private",
    )
    group = DocumentGroup(
        entity_id=entity_id, workspace_id=workspace_id, name="Shared files",
    )
    db_session.add_all([owner_membership, personal_folder, workspace_folder, group])
    await db_session.flush()
    shared = Document(
        entity_id=entity_id, name="shared.md", owner_id=creator["id"],
        folder_id=personal_folder.id, visibility="private", file_type="md",
    )
    artifact = Document(
        entity_id=entity_id, name="artifact.md", owner_id=creator["id"],
        folder_id=workspace_folder.id, visibility="private", file_type="md",
    )
    unrelated = Document(
        entity_id=entity_id, name="unrelated.md", owner_id=creator["id"],
        folder_id=personal_folder.id, visibility="private", file_type="md",
    )
    db_session.add_all([shared, artifact, unrelated])
    await db_session.flush()
    db_session.add(DocumentGroupMember(group_id=group.id, document_id=shared.id))
    await db_session.commit()

    owner_user = await db_session.get(User, owner["id"])
    assert owner_user is not None
    for document in (shared, artifact):
        assert await user_can_read_document(
            db_session, document, entity_id=entity_id, user_id=owner["id"],
        )
        assert await user_can_edit_document(db_session, document, user=owner_user)
        assert await user_can_share_document_externally(
            db_session, document, user=owner_user,
        )
        assert await _can_manage_document(db_session, owner_user, document)
        assert await _can_delete_document(db_session, owner_user, document)
        capabilities = await effective_document_capabilities_for_user(
            db_session, document=document, user_id=owner["id"],
        )
        assert {Capability.VIEW, Capability.EDIT, Capability.DELETE,
                Capability.GRANT_ACCESS, Capability.SHARE_EXTERNAL} <= capabilities
    ctx = await DocumentAccessContext.load(
        db_session, entity_id=entity_id, user_id=owner["id"],
    )
    await ctx.preload_documents(db_session, [shared, artifact, unrelated])
    assert await ctx.can_read_document(db_session, shared)
    assert await ctx.can_read_document(db_session, artifact)
    assert Capability.DELETE in await ctx.effective_document_capabilities(db_session, shared)
    assert await user_owns_workspace_folder(
        db_session, folder=workspace_folder, user_id=owner["id"],
    )
    assert not await user_owns_workspace_folder(
        db_session, folder=personal_folder, user_id=owner["id"],
    )
    assert not await ctx.can_read_document(db_session, unrelated)

    role_users = {
        role: await _create_entity_user(entity_id, f"workspace_file_{role}")
        for role in ("editor", "contributor", "viewer")
    }
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=workspace_id, user_id=actor["id"], role=role,
            status="active",
        )
        for role, actor in role_users.items()
    ])
    await db_session.commit()
    for role, actor in role_users.items():
        actor_user = await db_session.get(User, actor["id"])
        assert actor_user is not None
        assert await user_can_read_document(
            db_session, shared, entity_id=entity_id, user_id=actor["id"],
        )
        assert await user_can_read_document(
            db_session, artifact, entity_id=entity_id, user_id=actor["id"],
        )
        assert await user_can_edit_document(
            db_session, shared, user=actor_user,
        ) is (role == "editor")
        role_caps = await effective_document_capabilities_for_user(
            db_session, document=shared, user_id=actor["id"],
        )
        assert {Capability.VIEW, Capability.DOWNLOAD} <= role_caps
        assert (Capability.EDIT in role_caps) is (role == "editor")
        assert Capability.DELETE not in role_caps
        assert Capability.GRANT_ACCESS not in role_caps
        folder_caps = await workspace_folder_capabilities_for_user(
            db_session, folder=workspace_folder, user_id=actor["id"],
        )
        assert (Capability.UPLOAD_TO in folder_caps) is (role != "viewer")
        role_ctx = await DocumentAccessContext.load(
            db_session, entity_id=entity_id, user_id=actor["id"],
        )
        await role_ctx.preload_documents(db_session, [shared, artifact, unrelated])
        assert await role_ctx.can_read_document(db_session, shared)
        assert await role_ctx.effective_document_capabilities(db_session, shared) == role_caps
        assert not await role_ctx.can_read_document(db_session, unrelated)
        detail = await client.get(
            f"/api/v1/documents/{shared.id}", headers=actor["headers"],
        )
        assert detail.status_code == 200, detail.text
        assert set(detail.json()["current_user_capabilities"]) == role_caps

    denied_edit = await client.put(
        f"/api/v1/documents/{shared.id}/content",
        headers=role_users["viewer"]["headers"], json={"content": "no"},
    )
    assert denied_edit.status_code == 403, denied_edit.text
    denied_grant = await client.post(
        f"/api/v1/documents/{shared.id}/grants",
        headers=role_users["editor"]["headers"],
        json={
            "subject_type": "user", "subject_id": recipient["id"],
            "capabilities": [Capability.VIEW],
        },
    )
    assert denied_grant.status_code == 403, denied_grant.text

    contributor = role_users["contributor"]
    own_document = Document(
        entity_id=entity_id, name="contributor.md", owner_id=contributor["id"],
        folder_id=workspace_folder.id, visibility="private", file_type="md",
    )
    db_session.add(own_document)
    await db_session.commit()
    contributor_user = await db_session.get(User, contributor["id"])
    assert contributor_user is not None
    assert await user_can_edit_document(
        db_session, own_document, user=contributor_user,
    )
    assert await _can_delete_document(db_session, contributor_user, own_document)

    detail = await client.get(
        f"/api/v1/documents/{shared.id}", headers=owner["headers"],
    )
    assert detail.status_code == 200, detail.text
    assert Capability.DELETE in detail.json()["current_user_capabilities"]
    grant = await client.post(
        f"/api/v1/documents/{shared.id}/grants",
        headers=owner["headers"],
        json={
            "subject_type": "user", "subject_id": recipient["id"],
            "capabilities": [Capability.VIEW],
        },
    )
    assert grant.status_code == 201, grant.text
    trashed = await client.post(
        f"/api/v1/documents/{artifact.id}/trash", headers=owner["headers"],
    )
    assert trashed.status_code == 200, trashed.text

    owner_membership.role = "viewer"
    await db_session.commit()
    assert await user_can_read_document(
        db_session, shared, entity_id=entity_id, user_id=owner["id"],
    )
    assert not await _can_delete_document(db_session, owner_user, shared)
    owner_membership.status = "inactive"
    await db_session.commit()
    assert not await user_can_read_document(
        db_session, shared, entity_id=entity_id, user_id=owner["id"],
    )


@pytest.mark.asyncio
async def test_workspace_file_snapshot_does_not_truncate_ungrouped_artifacts(client, db_session):
    headers = await _auth(client, "file_snapshot_unbounded")
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "All artifacts"},
    )
    assert response.status_code == 201, response.text
    workspace = response.json()
    db_session.add_all([
        Document(
            id=generate_ulid(),
            entity_id=workspace["entity_id"],
            name=f"artifact-{index}.md",
            file_type="md",
            visibility="workspace",
            metadata_={"origin": {"workspace_id": workspace["id"]}},
        )
        for index in range(105)
    ])
    await db_session.commit()

    result = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/documents", headers=headers,
    )

    assert result.status_code == 200, result.text
    artifacts = next(
        group for group in result.json() if group["is_workspace_file_bucket"]
    )
    assert artifacts["document_count"] == 105
    assert len(artifacts["documents"]) == 105


@pytest.mark.asyncio
async def test_workspace_scope_accepts_canonical_metadata_and_storage_ownership(client, db_session):
    from packages.core.services.document_service import list_documents
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    headers = await _auth(client, "file_snapshot_scope")
    response = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Canonical scope"},
    )
    assert response.status_code == 201, response.text
    workspace = response.json()
    top_level = Document(
        id=generate_ulid(),
        entity_id=workspace["entity_id"],
        name="top-level.mp4",
        file_type="mp4",
        visibility="workspace",
        metadata_={"workspace_id": workspace["id"]},
    )
    physical = Document(
        id=generate_ulid(),
        entity_id=workspace["entity_id"],
        name="physical.mp4",
        file_type="mp4",
        visibility="workspace",
        fs_path=(
            f"{workspace_artifact_storage_base(workspace['artifact_folder_id'])}"
            "/physical.mp4"
        ),
    )
    unrelated = Document(
        id=generate_ulid(),
        entity_id=workspace["entity_id"],
        name="unrelated.mp4",
        file_type="mp4",
    )
    db_session.add_all([top_level, physical, unrelated])
    await db_session.commit()

    documents, total = await list_documents(
        db_session,
        workspace["entity_id"],
        workspace_id=workspace["id"],
        limit=None,
    )

    assert total == 2
    assert {document.id for document in documents} == {top_level.id, physical.id}

    result = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/documents", headers=headers,
    )
    assert result.status_code == 200, result.text
    artifacts = next(
        group for group in result.json() if group["is_workspace_file_bucket"]
    )
    assert {document["id"] for document in artifacts["documents"]} == {
        top_level.id,
        physical.id,
    }

    knowledge = await client.get(
        "/api/v1/documents/browse",
        params={"workspace_id": workspace["id"]},
        headers=headers,
    )
    assert knowledge.status_code == 200, knowledge.text
    assert {document["id"] for document in knowledge.json()["items"]} == {
        top_level.id,
        physical.id,
    }


@pytest.mark.asyncio
async def test_workspace_browse_shows_only_explicitly_projected_chat_uploads(
    client, db_session,
):
    owner_headers = await _auth(client, "workspace_chat_upload_browse")
    workspace_a = (await client.post(
        "/api/v1/workspaces", headers=owner_headers, json={"name": "Workspace A"},
    )).json()
    workspace_b = (await client.post(
        "/api/v1/workspaces", headers=owner_headers, json={"name": "Workspace B"},
    )).json()
    entity_id = workspace_a["entity_id"]
    member = await _create_entity_user(entity_id, "workspace_chat_upload_member")
    group_a, group_b = (
        DocumentGroup(entity_id=entity_id, workspace_id=workspace_a["id"], name="Workspace A uploads"),
        DocumentGroup(entity_id=entity_id, workspace_id=workspace_b["id"], name="Workspace B uploads"),
    )
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=workspace_a["id"], user_id=member["id"], role="member", status="active",
        ),
        group_a,
        group_b,
    ])
    await db_session.flush()

    def chat_upload(name: str, **kwargs) -> Document:
        attributes = {
            "id": generate_ulid(),
            "entity_id": entity_id,
            "name": name,
            "fs_path": f"uploads/chat/{name}",
            "file_type": name.rsplit(".", 1)[-1],
            "source": "chat_upload",
            "visibility": "workspace",
            "metadata_": {
                "origin": {
                    "workspace_id": workspace_a["id"],
                    "tool_name": "chat_upload",
                },
            },
        }
        attributes.update(kwargs)
        return Document(
            **attributes,
        )

    projected = [
        chat_upload("source.wav"),
        chat_upload("cover.png"),
        chat_upload("notes.txt"),
    ]
    trashed = chat_upload("deleted.txt", is_trashed=True, trashed_at=datetime.now(UTC))
    private = chat_upload("private.txt", visibility="private", owner_id="another-user")
    # Origin alone must not project a runtime upload into Workspace A.
    group_mismatch = chat_upload("other-group.txt")
    other_hidden_path = Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name="cache.md",
        fs_path=".cache/cache.md",
        file_type="md",
        source="upload",
        visibility="workspace",
        metadata_={"origin": {"workspace_id": workspace_a["id"]}},
    )
    db_session.add_all([*projected, trashed, private, group_mismatch, other_hidden_path])
    db_session.add_all([
        *(DocumentGroupMember(group_id=group_a.id, document_id=document.id) for document in [*projected, trashed, private, other_hidden_path]),
        DocumentGroupMember(group_id=group_b.id, document_id=group_mismatch.id),
    ])
    await db_session.commit()

    scoped = await client.get(
        "/api/v1/documents/browse",
        params={"workspace_id": workspace_a["id"]},
        headers=member["headers"],
    )
    assert scoped.status_code == 200, scoped.text
    assert {document["id"] for document in scoped.json()["items"]} == {
        document.id for document in projected
    }

    global_browse = await client.get(
        "/api/v1/documents/browse", headers=member["headers"],
    )
    assert global_browse.status_code == 200, global_browse.text
    assert not ({document.id for document in projected} & {
        document["id"] for document in global_browse.json()["items"]
    })

    nonmember = await _create_entity_user(entity_id, "workspace_chat_upload_nonmember")
    inaccessible = await client.get(
        "/api/v1/documents/browse",
        params={"workspace_id": workspace_a["id"]},
        headers=nonmember["headers"],
    )
    assert inaccessible.status_code == 200, inaccessible.text
    assert inaccessible.json()["items"] == []


@pytest.mark.parametrize(
    ("source", "fs_path", "group_workspace_ids", "expected"),
    [
        ("chat_upload", "uploads/chat/source.wav", {"workspace-a"}, True),
        # The final Gate may observe a later group binding; provenance alone
        # must not preserve the hidden-path exception.
        ("chat_upload", "uploads/chat/source.wav", {"workspace-b"}, False),
        ("upload", "uploads/chat/source.wav", {"workspace-a"}, False),
        ("chat_upload", ".cache/source.wav", {"workspace-a"}, False),
    ],
)
def test_workspace_browse_rechecks_final_chat_upload_projection(
    source, fs_path, group_workspace_ids, expected,
):
    from apps.api.routers.documents import _document_matches_browse_filters

    document = SimpleNamespace(
        source=source,
        fs_path=fs_path,
        name="source.wav",
        file_type="wav",
        mime_type="audio/wav",
        metadata_={"origin": {"workspace_id": "workspace-a"}},
    )
    final_gate_access_context = SimpleNamespace(
        document_group_workspace_ids=lambda _document: set(group_workspace_ids),
        document_workspace_ids=lambda _document: {"workspace-a"},
    )

    assert _document_matches_browse_filters(
        document,
        search_query=None,
        workspace_id="workspace-a",
        include_generated_assets=True,
        access_context=final_gate_access_context,
    ) is expected
