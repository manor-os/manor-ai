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
    effective_document_capabilities_for_user,
    folder_grant_capabilities_for_user,
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
        db.add_all([f_child, f_granted, f_member])
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

        folder_ids = [f_root.id, f_private.id, f_child.id, f_granted.id, f_member.id]
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
                assert ctx.can_read_folder(folder) == expected, (
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
        assert ctx_b.can_read_folder(folder_by_id[folder_ids[3]])  # granted folder
        assert not ctx_b.can_read_folder(folder_by_id[folder_ids[1]])  # private folder

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
