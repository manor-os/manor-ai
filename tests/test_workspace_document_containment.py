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
async def test_entity_admin_keeps_override(client: AsyncClient):
    owner_headers = await _auth(client, "wsdoc_admin_owner")
    owner = await _me(client, owner_headers)
    ws_id = await _make_workspace(owner["entity_id"], "Locked", owner["id"])
    doc_id = await _doc_in_workspace(owner["entity_id"], "secret.md", ws_id)

    admin = await _create_entity_user(owner["entity_id"], "wsdoc_admin", "admin")
    assert await _can_read(doc_id, owner["entity_id"], admin["id"], "admin")


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
