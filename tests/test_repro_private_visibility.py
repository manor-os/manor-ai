"""Regression tests for knowledge-base private visibility.

Original bug: with the (never-enabled) ``permissions_v1_enforce`` flag off,
the legacy authorize() branch denied capability-style actions for everyone —
so ``POST /permissions/documents/{id}/visibility`` 403'd even for the entity
owner, the "set private" save silently failed, and other entity members kept
seeing the file. The endpoints now use the same effective-capability model
as the rest of the document surface (owner / explicitly authorized curator /
explicit grant).

Covers:
  * owner can set a doc private; plain members lose browse/tree/GET/search
  * entity owner can still see another user's private files and folders
  * private folder (cascade) hides folder + children from plain members
  * no non-owner default role inherits visibility of another user's private
    files or folders
  * plain member without grant cannot change visibility (403)
  * member with a manage_metadata folder grant can change a child doc
  * foreign entity gets 404
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.test_document_permissions import (
    _auth,
    _create_entity_user,
    _invite_and_accept_member,
    _upload,
)


async def _entity_id(client: AsyncClient, headers: dict) -> str:
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return me.json()["entity_id"]


async def _set_visibility(
    client: AsyncClient, headers: dict, doc_id: str, visibility: str
):
    return await client.post(
        f"/api/v1/permissions/documents/{doc_id}/visibility",
        headers=headers,
        json={"visibility": visibility},
    )


@pytest.mark.asyncio
async def test_private_doc_and_folder_hidden_from_member(client: AsyncClient):
    owner_headers = await _auth(client, "priv_owner1")

    # Personal folders/files now default to private. Explicitly publish this
    # fixture first so the test can verify the later private transition.
    folder = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": "Secret Stuff"},
    )
    assert folder.status_code == 201, folder.text
    folder_id = folder.json()["id"]
    r = await client.post(
        f"/api/v1/folders/{folder_id}/properties",
        headers=owner_headers,
        json={"visibility": "entity", "cascade": False},
    )
    assert r.status_code == 200, r.text

    resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=owner_headers,
        files={"file": ("secret.md", b"top secret", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    doc_in_folder = resp.json()
    r = await _set_visibility(
        client, owner_headers, doc_in_folder["id"], "entity"
    )
    assert r.status_code == 200, r.text

    # Root doc, explicitly entity-visible so the member can see it at first
    root_doc = await _upload(
        client, owner_headers, name="root-secret.md", visibility="entity"
    )

    # Member joins the entity
    member_headers, _ = await _invite_and_accept_member(
        client, owner_headers, "priv.member1@test.com"
    )
    me = await client.get("/api/v1/auth/me", headers=member_headers)
    assert me.json()["role"] == "member"

    # Sanity: before privatizing, member sees both
    browse = await client.get("/api/v1/documents/browse", headers=member_headers)
    assert browse.status_code == 200, browse.text
    assert "root-secret.md" in {d["name"] for d in browse.json()["items"]}
    assert "Secret Stuff" in {f["name"] for f in browse.json()["folders"]}

    # Owner sets the doc private (endpoint used by DocumentPropertiesDialog)
    r = await _set_visibility(client, owner_headers, root_doc["id"], "private")
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "private"

    # Owner sets folder private with cascade (endpoint used by folder UI)
    r = await client.post(
        f"/api/v1/folders/{folder_id}/properties",
        headers=owner_headers,
        json={"visibility": "private", "cascade": True},
    )
    assert r.status_code == 200, r.text

    # Member browses root — both gone
    browse = await client.get("/api/v1/documents/browse", headers=member_headers)
    assert browse.status_code == 200, browse.text
    body = browse.json()
    assert "root-secret.md" not in {d["name"] for d in body["items"]}, (
        "private root doc leaked in browse"
    )
    assert "Secret Stuff" not in {f["name"] for f in body["folders"]}, (
        "private folder leaked in browse"
    )

    # Folder tree
    tree = await client.get("/api/v1/documents/folder-tree", headers=member_headers)
    assert tree.status_code == 200
    assert "Secret Stuff" not in {f["name"] for f in tree.json()}

    # Direct GETs
    r = await client.get(f"/api/v1/documents/{root_doc['id']}", headers=member_headers)
    assert r.status_code == 404, f"private doc readable directly: {r.status_code}"
    r = await client.get(
        f"/api/v1/documents/{doc_in_folder['id']}", headers=member_headers
    )
    assert r.status_code == 404, f"doc in private folder readable: {r.status_code}"

    # Search should not surface them either
    s = await client.get(
        "/api/v1/documents/browse?search=secret", headers=member_headers
    )
    assert s.status_code == 200
    assert "root-secret.md" not in {d["name"] for d in s.json()["items"]}
    assert "Secret Stuff" not in {f["name"] for f in s.json()["folders"]}

    # Owner still sees everything
    browse = await client.get("/api/v1/documents/browse", headers=owner_headers)
    assert "root-secret.md" in {d["name"] for d in browse.json()["items"]}
    assert "Secret Stuff" in {f["name"] for f in browse.json()["folders"]}


@pytest.mark.asyncio
async def test_owner_sees_other_users_private_knowledge(client: AsyncClient):
    """Entity owner is the sole default cross-user Knowledge reader."""
    owner_headers = await _auth(client, "priv_owner_cross_user")
    member_headers, _ = await _invite_and_accept_member(
        client,
        owner_headers,
        "priv.cross.user@test.com",
    )

    folder_name = "Member Private Folder"
    folder = await client.post(
        "/api/v1/documents/folders",
        headers=member_headers,
        json={"name": folder_name},
    )
    assert folder.status_code == 201, folder.text

    doc_name = "member-private.md"
    document = await client.post(
        f"/api/v1/documents/upload?folder_id={folder.json()['id']}",
        headers=member_headers,
        files={"file": (doc_name, b"member private", "text/markdown")},
    )
    assert document.status_code == 201, document.text

    browse = await client.get("/api/v1/documents/browse", headers=owner_headers)
    assert browse.status_code == 200, browse.text
    assert folder_name in {f["name"] for f in browse.json()["folders"]}

    direct = await client.get(
        f"/api/v1/documents/{document.json()['id']}",
        headers=owner_headers,
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["name"] == doc_name


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
@pytest.mark.asyncio
async def test_non_owner_roles_do_not_see_owner_private_knowledge(
    client: AsyncClient,
    role: str,
):
    """Only owner has default entity-wide visibility into private Knowledge."""
    owner_headers = await _auth(client, f"priv_owner2_{role}")
    entity_id = await _entity_id(client, owner_headers)

    folder_name = f"Owner Only {role}"
    folder = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": folder_name},
    )
    assert folder.status_code == 201, folder.text

    doc_name = f"owner-only-{role}.md"
    doc = await _upload(client, owner_headers, name=doc_name)
    r = await _set_visibility(client, owner_headers, doc["id"], "private")
    assert r.status_code == 200, r.text

    other_user = await _create_entity_user(
        entity_id,
        f"priv_{role}2",
        role=role,
    )
    r = await client.get(
        f"/api/v1/documents/{doc['id']}",
        headers=other_user["headers"],
    )
    assert r.status_code == 404, f"{role} role leaked an owner's private doc"

    browse = await client.get(
        "/api/v1/documents/browse",
        headers=other_user["headers"],
    )
    assert browse.status_code == 200, browse.text
    assert doc_name not in {d["name"] for d in browse.json()["items"]}
    assert folder_name not in {f["name"] for f in browse.json()["folders"]}

    tree = await client.get(
        "/api/v1/documents/folder-tree",
        headers=other_user["headers"],
    )
    assert tree.status_code == 200, tree.text
    assert folder_name not in {f["name"] for f in tree.json()}

    # Nor may a non-owner mutate another user's private document without an
    # explicit grant, even when their default role is admin.
    r = await _set_visibility(
        client,
        other_user["headers"],
        doc["id"],
        "entity",
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_member_without_grant_cannot_change_visibility(client: AsyncClient):
    owner_headers = await _auth(client, "priv_owner3")
    doc = await _upload(
        client, owner_headers, name="entity-doc.md", visibility="entity"
    )

    member_headers, _ = await _invite_and_accept_member(
        client, owner_headers, "priv.member3@test.com"
    )
    r = await _set_visibility(client, member_headers, doc["id"], "private")
    assert r.status_code == 403, r.text

    # Doc unchanged
    r = await client.get(f"/api/v1/documents/{doc['id']}", headers=owner_headers)
    assert r.json()["visibility"] == "entity"


@pytest.mark.asyncio
async def test_folder_grant_allows_metadata_change_within_folder_visibility(
    client: AsyncClient,
):
    """A manage_metadata grant on a folder covers child documents."""
    owner_headers = await _auth(client, "priv_owner4")

    folder = await client.post(
        "/api/v1/documents/folders",
        headers=owner_headers,
        json={"name": "Curated"},
    )
    assert folder.status_code == 201, folder.text
    folder_id = folder.json()["id"]

    resp = await client.post(
        f"/api/v1/documents/upload?folder_id={folder_id}",
        headers=owner_headers,
        files={"file": ("curated.md", b"curated", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()

    member_headers, member_data = await _invite_and_accept_member(
        client, owner_headers, "priv.member4@test.com"
    )

    # Without a grant: 403
    r = await _set_visibility(client, member_headers, doc["id"], "workspace")
    assert r.status_code == 403, r.text

    grant = await client.post(
        f"/api/v1/folders/{folder_id}/grants",
        headers=owner_headers,
        json={
            "subject_type": "user",
            "subject_id": member_data["user_id"],
            "capabilities": ["view", "manage_metadata"],
        },
    )
    assert grant.status_code == 201, grant.text

    # The grant authorizes metadata changes, but cannot publish a child more
    # broadly than its still-private parent folder.
    r = await _set_visibility(client, member_headers, doc["id"], "workspace")
    assert r.status_code == 409, r.text

    r = await _set_visibility(client, member_headers, doc["id"], "private")
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_foreign_entity_visibility_change_404(client: AsyncClient):
    owner_headers = await _auth(client, "priv_owner5")
    doc = await _upload(client, owner_headers, name="mine.md")

    outsider_headers = await _auth(client, "priv_outsider5")
    r = await _set_visibility(client, outsider_headers, doc["id"], "private")
    assert r.status_code == 404, r.text
