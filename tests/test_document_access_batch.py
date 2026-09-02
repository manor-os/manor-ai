"""Parity pin: DocumentAccessContext must match the single-item access helpers.

The Knowledge listing endpoints (browse / folder-tree / counts) evaluate
document and folder readability through the batched ``DocumentAccessContext``,
while single-document endpoints still go through ``user_can_read_document`` /
``user_can_read_folder``. If the two ever disagree, a document can appear in a
list the user cannot open — or vanish from a list they can. This test builds a
matrix of visibility/ownership/grant/workspace cases and asserts, for every
(user, resource) pair, that the batch evaluator returns exactly what the
single-item helper returns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.document import (
    Document,
    DocumentFolder,
    DocumentGroup,
    DocumentGroupMember,
)
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.services.document_access import (
    DocumentAccessContext,
    document_is_client_visible,
    effective_document_capabilities_for_user,
    folder_grant_capabilities_for_user,
    resolve_document_policy_lock_scope,
    unreadable_document_paths,
    user_can_read_document,
    user_can_read_folder,
    visible_document_counts_by_folder,
)
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _folder(entity_id: str, name: str, **kwargs) -> DocumentFolder:
    return DocumentFolder(id=generate_ulid(), entity_id=entity_id, name=name, **kwargs)


def _doc(entity_id: str, name: str, **kwargs) -> Document:
    return Document(
        id=generate_ulid(),
        entity_id=entity_id,
        name=name,
        file_type="md",
        source="upload",
        **kwargs,
    )


def _grant(entity_id: str, resource_type: str, resource_id: str, subject_id: str, capabilities: list[str]) -> ResourceGrant:
    return ResourceGrant(
        id=generate_ulid(),
        entity_id=entity_id,
        resource_type=resource_type,
        resource_id=resource_id,
        subject_type=SubjectType.USER,
        subject_id=subject_id,
        capabilities=capabilities,
        granted_at=datetime.now(timezone.utc),
        status=GrantStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_visible_document_pagination_scans_past_denied_batches(monkeypatch):
    from packages.core.services import document_access
    from packages.core.services import document_service

    documents = [
        SimpleNamespace(id=f"doc-{index}", allowed=index >= 2100)
        for index in range(2500)
    ]
    raw_offsets: list[int] = []

    async def list_documents(*_args, limit, offset, **_kwargs):
        raw_offsets.append(offset)
        return documents[offset : offset + limit], len(documents)

    async def readable(_db, rows, *, stat_files):
        assert stat_files is False
        return rows

    class FakeContext:
        async def preload_documents(self, _db, _documents):
            return None

        async def can_read_document(self, _db, document, **_kwargs):
            return document.allowed

    async def load_context(_cls, _db, **_kwargs):
        return FakeContext()

    monkeypatch.setattr(document_service, "list_documents", list_documents)
    monkeypatch.setattr(
        document_access,
        "_filter_readable_local_documents",
        readable,
    )
    monkeypatch.setattr(
        document_access.DocumentAccessContext,
        "load",
        classmethod(load_context),
    )

    page, total = await document_access.list_visible_documents(
        object(),
        "entity-1",
        user_id="user-1",
        role="member",
        limit=100,
        offset=0,
    )

    assert [document.id for document in page] == [
        f"doc-{index}" for index in range(2100, 2200)
    ]
    assert total == 400
    assert raw_offsets == [0, 2000]


@pytest.mark.asyncio
async def test_userless_agent_document_listing_fails_closed(monkeypatch):
    from packages.core.services import document_access
    from packages.core.services import document_service

    async def unexpected_list(*_args, **_kwargs):
        raise AssertionError("a userless agent must not query entity documents")

    monkeypatch.setattr(document_service, "list_documents", unexpected_list)

    page, total = await document_access.list_visible_documents(
        object(),
        "entity-1",
        user_id=None,
        actor_type="agent",
    )

    assert page == []
    assert total == 0


@pytest.mark.asyncio
async def test_userless_document_pagination_filters_before_slicing(monkeypatch):
    from packages.core.services import document_access
    from packages.core.services import document_service

    documents = [
        SimpleNamespace(id="hidden", entity_id="entity-1"),
        SimpleNamespace(id="visible-1", entity_id="entity-1"),
        SimpleNamespace(id="visible-2", entity_id="entity-1"),
    ]

    async def list_documents(*_args, limit, offset, **_kwargs):
        return documents[offset : offset + limit], len(documents)

    async def readable(_db, rows, *, stat_files):
        assert stat_files is False
        return rows

    class FakeContext:
        async def preload_documents(self, _db, _documents):
            return None

        async def can_read_document(self, _db, document, **_kwargs):
            return document.id != "hidden"

    async def load_context(_cls, _db, **_kwargs):
        return FakeContext()

    monkeypatch.setattr(document_service, "list_documents", list_documents)
    monkeypatch.setattr(
        document_access,
        "_filter_readable_local_documents",
        readable,
    )
    monkeypatch.setattr(
        document_access.DocumentAccessContext,
        "load",
        classmethod(load_context),
    )

    page, total = await document_access.list_visible_documents(
        object(),
        "entity-1",
        user_id=None,
        actor_type="user",
        limit=1,
        offset=0,
    )

    assert [document.id for document in page] == ["visible-1"]
    assert total == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["list", "usage", "counts"])
async def test_bounded_document_surfaces_scope_context_to_candidate_batch(
    monkeypatch,
    surface: str,
):
    from packages.core.services import document_access, document_service

    document = SimpleNamespace(
        id="doc-a",
        entity_id="entity-1",
        folder_id="folder-a",
        file_size=7,
    )

    async def list_documents(*_args, limit, offset, **_kwargs):
        return ([document], 1) if offset == 0 else ([], 1)

    async def readable(_db, rows, *, stat_files):
        assert stat_files is False
        return rows

    loaded_scopes: list[tuple[set[str], set[str]]] = []

    class FakeContext:
        async def preload_documents(self, _db, _documents):
            return None

        async def can_read_document(self, _db, _document, **_kwargs):
            return True

    async def load_context(_cls, _db, **kwargs):
        loaded_scopes.append((kwargs["folder_ids"], kwargs["document_ids"]))
        return FakeContext()

    monkeypatch.setattr(document_service, "list_documents", list_documents)
    monkeypatch.setattr(document_access, "_filter_readable_local_documents", readable)
    monkeypatch.setattr(
        document_access.DocumentAccessContext,
        "load",
        classmethod(load_context),
    )

    if surface == "list":
        await document_access.list_visible_documents(
            object(),
            "entity-1",
            user_id=None,
        )
    elif surface == "usage":
        await document_access.visible_storage_usage(
            object(),
            "entity-1",
            user_id=None,
        )
    else:
        await document_access.visible_document_counts_by_folder(
            object(),
            "entity-1",
            folder_ids={"folder-a"},
            user_id=None,
        )

    assert loaded_scopes == [({"folder-a"}, {"doc-a"})]


@pytest.mark.asyncio
async def test_folder_counts_authorize_documents_in_bounded_batches(monkeypatch):
    from packages.core.services import document_access, document_service

    documents = [
        SimpleNamespace(
            id=f"doc-{index}",
            entity_id="entity-1",
            folder_id="folder-a",
        )
        for index in range(2_501)
    ]
    list_calls: list[tuple[int, int, int]] = []

    async def list_documents(*_args, folder_ids, limit, offset, **_kwargs):
        list_calls.append((len(folder_ids), limit, offset))
        return documents[offset:offset + limit], len(documents)

    async def readable(_db, rows, *, stat_files):
        assert stat_files is False
        return rows

    authorized_batch_sizes: list[int] = []

    async def authorize_batch(rows):
        authorized_batch_sizes.append(len(rows))
        return rows

    monkeypatch.setattr(document_service, "list_documents", list_documents)
    monkeypatch.setattr(document_access, "_filter_readable_local_documents", readable)

    counts = await visible_document_counts_by_folder(
        object(),
        "entity-1",
        folder_ids={"folder-a"},
        authorize_batch=authorize_batch,
    )

    assert counts == {"folder-a": 2_501}
    assert authorized_batch_sizes == [2_000, 501]
    assert list_calls == [(1, 2_000, 0), (1, 2_000, 2_000)]


@pytest.mark.asyncio
async def test_folder_counts_bound_each_folder_id_query(monkeypatch):
    from packages.core.services import document_service

    folder_batch_sizes: list[int] = []

    async def list_documents(*_args, folder_ids, **_kwargs):
        folder_batch_sizes.append(len(folder_ids))
        return [], 0

    monkeypatch.setattr(document_service, "list_documents", list_documents)

    counts = await visible_document_counts_by_folder(
        object(),
        "entity-1",
        folder_ids={f"folder-{index}" for index in range(1_201)},
    )

    assert counts == {}
    assert sorted(folder_batch_sizes) == [201, 500, 500]


@pytest.mark.asyncio
async def test_batch_context_matches_single_item_helpers(client: AsyncClient):
    owner_headers = await _auth(client, "batchparity_owner")
    me = await _me(client, owner_headers)
    entity_id = me["entity_id"]
    owner_id = me["id"]

    member_a = await _create_entity_user(entity_id, "batchparity_a", role="member")
    member_b = await _create_entity_user(entity_id, "batchparity_b", role="member")
    viewer = await _create_entity_user(entity_id, "batchparity_v", role="viewer")
    client_user = await _create_entity_user(entity_id, "batchparity_c", role="client")

    async with db_module.async_session() as db:
        # Workspace that only member A belongs to.
        ws = Workspace(entity_id=entity_id, name="Batch WS", settings={"access_mode": "members_only"})
        db.add(ws)
        await db.flush()
        db.add(WorkspaceStaff(workspace_id=ws.id, staff_id=None, user_id=member_a["id"], role="owner", status="active"))

        f_root = _folder(entity_id, "Root", visibility="entity")
        f_private = _folder(entity_id, "Private", visibility="private", owner_id=owner_id)
        db.add_all([f_root, f_private])
        await db.flush()
        # Entity-visible child under a private parent — the path ceiling case.
        f_child = _folder(entity_id, "Child", visibility="entity", parent_id=f_private.id)
        # Private folder where member B holds an upload_to grant.
        f_granted = _folder(entity_id, "Granted", visibility="private", owner_id=owner_id)
        f_member = _folder(entity_id, "Mine", visibility="private", owner_id=member_a["id"])
        f_confidential = _folder(
            entity_id,
            "Legacy Confidential",
            visibility="entity",
            classification="confidential",
            client_visible=False,
        )
        db.add_all([f_child, f_granted, f_member, f_confidential])
        await db.flush()

        docs = {
            "entity": _doc(entity_id, "entity.md", visibility="entity", folder_id=f_root.id),
            "private": _doc(entity_id, "private.md", visibility="private", owner_id=owner_id, folder_id=f_root.id),
            "owned_by_a": _doc(entity_id, "mine.md", visibility="private", owner_id=member_a["id"]),
            "in_private_folder": _doc(entity_id, "ceiling.md", visibility="entity", folder_id=f_child.id),
            "folder_grant": _doc(entity_id, "granted.md", visibility="private", folder_id=f_granted.id),
            "direct_grant": _doc(entity_id, "direct.md", visibility="private", owner_id=owner_id),
            "quarantined": _doc(entity_id, "bad.md", visibility="entity", quarantine_status="quarantined"),
            "workspace": _doc(entity_id, "ws.md", visibility="workspace"),
            "entity_in_ws": _doc(entity_id, "wsnet.md", visibility="entity"),
            "client_visible": _doc(entity_id, "client.md", visibility="entity", client_visible=True),
            # Legacy/cascade=false drift: the row is still internal/client
            # visible, but its effective folder policy must win at read time.
            "under_confidential": _doc(
                entity_id,
                "legacy-child.md",
                visibility="entity",
                classification="internal",
                client_visible=True,
                folder_id=f_confidential.id,
            ),
        }
        db.add_all(docs.values())
        await db.flush()

        group = DocumentGroup(id=generate_ulid(), entity_id=entity_id, name="ws-net", workspace_id=ws.id)
        db.add(group)
        await db.flush()
        db.add_all([
            DocumentGroupMember(group_id=group.id, document_id=docs["workspace"].id),
            DocumentGroupMember(group_id=group.id, document_id=docs["entity_in_ws"].id),
        ])
        db.add_all([
            # view on the folder cascades document read; upload_to alone reads the folder but not its docs
            _grant(entity_id, ResourceType.DOCUMENT_FOLDER, f_granted.id, member_b["id"], [Capability.VIEW]),
            _grant(entity_id, ResourceType.DOCUMENT, docs["direct_grant"].id, member_b["id"], [Capability.VIEW]),
        ])
        await db.commit()

        folder_ids = [
            f_root.id,
            f_private.id,
            f_child.id,
            f_granted.id,
            f_member.id,
            f_confidential.id,
        ]
        doc_ids = {key: d.id for key, d in docs.items()}

    users = [
        {"id": owner_id, "role": "owner"},
        {"id": member_a["id"], "role": "member"},
        {"id": member_b["id"], "role": "member"},
        {"id": viewer["id"], "role": "viewer"},
        {"id": client_user["id"], "role": "client"},
        {"id": None, "role": None},  # background caller
    ]

    async with db_module.async_session() as db:
        from sqlalchemy import select

        folder_rows = (await db.execute(
            select(DocumentFolder).where(DocumentFolder.entity_id == entity_id)
        )).scalars().all()
        folder_by_id = {f.id: f for f in folder_rows}
        doc_rows = (await db.execute(
            select(Document).where(Document.entity_id == entity_id)
        )).scalars().all()
        doc_by_id = {d.id: d for d in doc_rows}

        for u in users:
            ctx = await DocumentAccessContext.load(
                db, entity_id=entity_id, user_id=u["id"], role=u["role"],
            )
            await ctx.preload_documents(db, doc_rows)

            for folder_id in folder_ids:
                folder = folder_by_id[folder_id]
                expected = await user_can_read_folder(
                    db, folder, entity_id=entity_id, user_id=u["id"], role=u["role"],
                )
                assert await ctx.can_read_folder(db, folder) == expected, (
                    f"folder read mismatch user={u} folder={folder.name}"
                )
                if u["id"]:
                    expected_caps = await folder_grant_capabilities_for_user(
                        db, entity_id=entity_id, folder_id=folder_id, user_id=u["id"],
                    )
                    assert ctx.folder_capabilities(folder_id) == expected_caps, (
                        f"folder caps mismatch user={u} folder={folder.name}"
                    )

            for key, doc_id in doc_ids.items():
                document = doc_by_id[doc_id]
                expected = await user_can_read_document(
                    db, document, entity_id=entity_id, user_id=u["id"], role=u["role"],
                )
                actual = await ctx.can_read_document(db, document)
                assert actual == expected, (
                    f"doc read mismatch user={u} doc={key}: batch={actual} single={expected}"
                )
                if u["id"]:
                    expected_caps = await effective_document_capabilities_for_user(
                        db, document=document, user_id=u["id"], role=u["role"],
                    )
                    actual_caps = await ctx.effective_document_capabilities(db, document)
                    assert actual_caps == expected_caps, (
                        f"doc caps mismatch user={u} doc={key}"
                    )

        # Hard assertions so a bug shared by both implementations can't hide.
        ctx_b = await DocumentAccessContext.load(
            db, entity_id=entity_id, user_id=member_b["id"], role="member",
        )
        await ctx_b.preload_documents(db, doc_rows)
        assert not await ctx_b.can_read_document(db, doc_by_id[doc_ids["private"]])
        assert await ctx_b.can_read_document(db, doc_by_id[doc_ids["direct_grant"]])
        assert await ctx_b.can_read_document(db, doc_by_id[doc_ids["folder_grant"]])
        assert not await ctx_b.can_read_document(db, doc_by_id[doc_ids["in_private_folder"]])
        assert not await ctx_b.can_read_document(db, doc_by_id[doc_ids["workspace"]])
        assert not await ctx_b.can_read_document(db, doc_by_id[doc_ids["entity_in_ws"]])
        assert not await ctx_b.can_read_document(db, doc_by_id[doc_ids["quarantined"]])
        assert await ctx_b.can_read_folder(db, folder_by_id[folder_ids[3]])  # granted folder
        assert not await ctx_b.can_read_folder(db, folder_by_id[folder_ids[1]])  # private folder

        ctx_client = await DocumentAccessContext.load(
            db, entity_id=entity_id, user_id=client_user["id"], role="client",
        )
        await ctx_client.preload_documents(db, doc_rows)
        legacy_child = doc_by_id[doc_ids["under_confidential"]]
        assert not await ctx_client.can_read_document(db, legacy_child)
        assert not await document_is_client_visible(
            db,
            legacy_child,
            entity_id=entity_id,
        )

        ctx_a = await DocumentAccessContext.load(
            db, entity_id=entity_id, user_id=member_a["id"], role="member",
        )
        await ctx_a.preload_documents(db, doc_rows)
        assert await ctx_a.can_read_document(db, doc_by_id[doc_ids["workspace"]])
        assert await ctx_a.can_read_document(db, doc_by_id[doc_ids["entity_in_ws"]])
        assert await ctx_a.can_read_document(db, doc_by_id[doc_ids["owned_by_a"]])

        # Counts endpoint: batch counts equal a brute-force single-item count.
        counts = await visible_document_counts_by_folder(
            db, entity_id,
            folder_ids=set(folder_ids),
            user_id=member_b["id"],
            role="member",
        )
        expected_counts: dict[str, int] = {}
        for document in doc_by_id.values():
            fid = document.folder_id
            if fid not in set(folder_ids):
                continue
            if await user_can_read_document(
                db, document, entity_id=entity_id, user_id=member_b["id"], role="member",
            ):
                expected_counts[fid] = expected_counts.get(fid, 0) + 1
        assert counts == expected_counts


@pytest.mark.asyncio
async def test_batch_context_hard_stops_deleted_workspace_documents(
    client: AsyncClient,
):
    from packages.core.services.document_access import list_visible_documents

    owner_headers = await _auth(client, "batchdeleted_owner")
    me = await _me(client, owner_headers)
    entity_id = me["entity_id"]
    owner_id = me["id"]

    async with db_module.async_session() as db:
        root = _folder(entity_id, "Deleted Workspace", visibility="entity")
        db.add(root)
        await db.flush()
        workspace = Workspace(
            entity_id=entity_id,
            name="Deleted Workspace",
            artifact_folder_id=root.id,
            deleted_at=datetime.now(timezone.utc),
        )
        db.add(workspace)
        await db.flush()
        nested = _folder(
            entity_id,
            "Nested",
            visibility="entity",
            parent_id=root.id,
        )
        folder_document = _doc(
            entity_id,
            "folder-owned.md",
            visibility="entity",
            owner_id=owner_id,
            folder_id=nested.id,
        )
        provenance_document = _doc(
            entity_id,
            "provenance-owned.md",
            visibility="entity",
            owner_id=owner_id,
            metadata_={"origin": {"workspace_id": workspace.id}},
        )
        physical_path = f"Workspaces/_by_id/{root.id}/physical-owned.md"
        physical_document = _doc(
            entity_id,
            "physical-owned.md",
            visibility="entity",
            client_visible=True,
            owner_id=owner_id,
            fs_path=physical_path,
        )
        db.add_all([
            nested,
            folder_document,
            provenance_document,
            physical_document,
        ])
        await db.commit()

        scope = await resolve_document_policy_lock_scope(db, physical_document)
        assert workspace.id in scope.workspace_ids

        documents = [folder_document, provenance_document, physical_document]
        for user_id, role in ((owner_id, "owner"), (None, None)):
            ctx = await DocumentAccessContext.load(
                db,
                entity_id=entity_id,
                user_id=user_id,
                role=role,
            )
            await ctx.preload_documents(db, documents)
            assert not await ctx.can_read_folder(db, root)
            assert not await ctx.can_read_folder(db, nested)
            for document in documents:
                assert not await ctx.can_read_document(db, document)
            if user_id:
                for document in documents:
                    assert await ctx.effective_document_capabilities(
                        db,
                        document,
                    ) == set()

        assert not await document_is_client_visible(
            db,
            physical_document,
            entity_id=entity_id,
        )
        unprojected_path = f"Workspaces/_by_id/{root.id}/unprojected.txt"
        assert await unreadable_document_paths(
            db,
            entity_id=entity_id,
            rel_paths=[physical_path, unprojected_path],
            user_id=owner_id,
            role="owner",
        ) == {physical_path, unprojected_path}
        assert await unreadable_document_paths(
            db,
            entity_id=entity_id,
            rel_paths=[physical_path, unprojected_path],
            user_id=None,
            actor_type="user",
        ) == {physical_path, unprojected_path}

        listed, total = await list_visible_documents(
            db,
            entity_id,
            user_id=None,
            limit=None,
        )
        assert listed == []
        assert total == 0


@pytest.mark.asyncio
async def test_physical_workspace_owner_constrains_active_document(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "physicalowner")
    owner = await _me(client, owner_headers)
    outsider = await _create_entity_user(
        owner["entity_id"],
        "physicaloutsider",
        "member",
    )

    async with db_module.async_session() as db:
        root = _folder(owner["entity_id"], "Physical owner", visibility="entity")
        db.add(root)
        await db.flush()
        workspace = Workspace(
            entity_id=owner["entity_id"],
            name="Physical owner",
            artifact_folder_id=root.id,
            settings={"access_mode": "members_only"},
        )
        document = _doc(
            owner["entity_id"],
            "physical.md",
            visibility="entity",
            fs_path=f"Workspaces/_by_id/{root.id}/physical.md",
        )
        db.add_all([workspace, document])
        await db.commit()

        assert not await user_can_read_document(
            db,
            document,
            entity_id=owner["entity_id"],
            user_id=outsider["id"],
            role="member",
        )
        ctx = await DocumentAccessContext.load(
            db,
            entity_id=owner["entity_id"],
            user_id=outsider["id"],
            role="member",
        )
        await ctx.preload_documents(db, [document])
        assert not await ctx.can_read_document(db, document)
