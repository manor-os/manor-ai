"""E2E tests for folder-level permissions (RFC §13.3, Phase B).

Covers:
  * POST /folders/{id}/properties — set visibility/classification/client_visible
    + cascade option that auto-adjusts existing docs + subfolders
  * Folder Grants CRUD (resource_type='document_folder')
  * Folder Shares CRUD
  * Invariants:
      - Upload into a Confidential folder auto-upgrades child Internal -> Confidential
      - Move into a higher-classification folder auto-upgrades the doc
      - Restricted folder cannot be public-visible (400)
      - Confidential+ folder forces children non-client_visible
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import pytest
from httpx import AsyncClient

import packages.core.database as db_module


async def _auth(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _invite_and_accept_member(
    client: AsyncClient,
    owner_headers: dict,
    email: str,
    *,
    name: str = "Team Member",
) -> tuple[dict, dict]:
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": email, "name": name},
    )
    assert invite.status_code == 201, invite.text
    invite_data = invite.json()
    accepted = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "memberpass123",
            "username": name,
            "invite_token": invite_data["invite_token"],
        },
    )
    assert accepted.status_code == 200, accepted.text
    data = accepted.json()
    data["staff_id"] = invite_data["staff_id"]
    return {"Authorization": f"Bearer {data['access_token']}"}, data


async def _create_folder(
    client: AsyncClient,
    headers: dict,
    name: str,
    parent_id: str | None = None,
) -> dict:
    resp = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": name, "parent_id": parent_id},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _upload(
    client: AsyncClient,
    headers: dict,
    *,
    folder_id: str | None = None,
    visibility: str | None = None,
    classification: str | None = None,
    name: str = "doc.md",
) -> dict:
    params: list[tuple[str, str]] = []
    if folder_id:
        params.append(("folder_id", folder_id))
    if visibility:
        params.append(("visibility", visibility))
    if classification:
        params.append(("classification", classification))
    url = "/api/v1/documents/upload"
    if params:
        url = f"{url}?{urlencode(params)}"
    resp = await client.post(
        url,
        headers=headers,
        files={"file": (name, b"hello", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Folder properties endpoint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_folder_properties_basic(client: AsyncClient):
    headers = await _auth(client, "foldprops")
    folder = await _create_folder(client, headers, "Contracts")
    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={
            "visibility": "workspace",
            "classification": "confidential",
            "cascade": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["visibility"] == "workspace"
    assert body["classification"] == "confidential"
    assert body["cascade_summary"]["docs_updated"] == 0
    assert body["cascade_summary"]["subfolders_updated"] == 0


@pytest.mark.asyncio
async def test_folder_properties_validate_final_state_across_partial_updates(client: AsyncClient):
    headers = await _auth(client, "foldfinal")
    folder = await _create_folder(client, headers, "Final State")
    first = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"visibility": "private", "classification": "restricted", "cascade": False},
    )
    assert first.status_code == 200, first.text
    invalid = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"visibility": "public", "cascade": False},
    )
    assert invalid.status_code == 400

    internal = await _create_folder(client, headers, "Client Visible")
    await client.post(
        f"/api/v1/folders/{internal['id']}/properties",
        headers=headers,
        json={"client_visible": True, "cascade": False},
    )
    tightened = await client.post(
        f"/api/v1/folders/{internal['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": False},
    )
    assert tightened.status_code == 200, tightened.text
    assert tightened.json()["client_visible"] is False


@pytest.mark.asyncio
async def test_set_folder_properties_cascade(client: AsyncClient):
    """Cascade=true auto-adjusts existing docs + subfolders to new floor/ceiling."""
    headers = await _auth(client, "foldcasc")
    parent = await _create_folder(client, headers, "Q3")
    sub = await _create_folder(client, headers, "Clients", parent_id=parent["id"])
    # Start with the docs at the defaults (entity / internal) — cascade
    # will then upgrade them to confidential when parent locks down.
    doc = await _upload(
        client,
        headers,
        folder_id=parent["id"],
        name="q3-plan.md",
    )
    assert doc["classification"] == "internal"

    # Lock parent down to confidential.
    resp = await client.post(
        f"/api/v1/folders/{parent['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": True},
    )
    assert resp.status_code == 200, resp.text
    summary = resp.json()["cascade_summary"]
    assert summary["docs_updated"] >= 1

    # Re-fetch the doc — should be confidential now.
    refreshed = (await client.get(f"/api/v1/documents/{doc['id']}", headers=headers)).json()
    assert refreshed["classification"] == "confidential"
    # subfolder loaded via list — should also be at least confidential.
    subs = (await client.get("/api/v1/documents/folders", headers=headers)).json()
    sub_row = next(f for f in subs if f["id"] == sub["id"])
    assert sub_row["classification"] == "confidential"


@pytest.mark.asyncio
async def test_folder_policy_rejects_unsafe_non_cascade_update(client: AsyncClient):
    headers = await _auth(client, "foldnocascade")
    folder = await _create_folder(client, headers, "Existing Contents")
    doc = await _upload(
        client,
        headers,
        folder_id=folder["id"],
        classification="internal",
        name="existing.md",
    )

    rejected = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": False},
    )
    assert rejected.status_code == 409

    unchanged = await client.get(f"/api/v1/documents/{doc['id']}", headers=headers)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["classification"] == "internal"


@pytest.mark.asyncio
async def test_folder_restricted_public_blocked(client: AsyncClient):
    headers = await _auth(client, "foldinv")
    folder = await _create_folder(client, headers, "Vault")
    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"visibility": "public", "classification": "restricted"},
    )
    assert resp.status_code == 400
    assert "Restricted" in resp.text


# ── Upload/move invariants ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_into_confidential_folder_auto_upgrades(client: AsyncClient):
    headers = await _auth(client, "upinv")
    folder = await _create_folder(client, headers, "Confidential")
    await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": False},
    )

    # Upload as internal — should auto-upgrade to confidential.
    doc = await _upload(
        client,
        headers,
        folder_id=folder["id"],
        classification="internal",
        name="upgraded.md",
    )
    assert doc["classification"] == "confidential"


@pytest.mark.asyncio
async def test_move_into_higher_classification_folder_auto_upgrades(client: AsyncClient):
    headers = await _auth(client, "moveinv")
    open_f = await _create_folder(client, headers, "Open")
    locked_f = await _create_folder(client, headers, "Locked")
    await client.post(
        f"/api/v1/folders/{locked_f['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": False},
    )
    # Upload an internal doc into Open
    doc = await _upload(
        client,
        headers,
        folder_id=open_f["id"],
        classification="internal",
        name="moveme.md",
    )
    assert doc["classification"] == "internal"
    # Move into Locked
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/move",
        headers=headers,
        json={"folder_id": locked_f["id"]},
    )
    assert resp.status_code == 200, resp.text
    moved = resp.json()
    assert moved["classification"] == "confidential"
    assert moved["folder_id"] == locked_f["id"]


@pytest.mark.asyncio
async def test_moving_folder_reapplies_parent_policy_to_subtree(client: AsyncClient):
    headers = await _auth(client, "movefolderpolicy")
    locked = await _create_folder(client, headers, "Locked Parent")
    await client.post(
        f"/api/v1/folders/{locked['id']}/properties",
        headers=headers,
        json={"visibility": "workspace", "classification": "confidential", "cascade": False},
    )
    moving = await _create_folder(client, headers, "Moving")
    child = await _create_folder(client, headers, "Child", parent_id=moving["id"])
    doc = await _upload(
        client,
        headers,
        folder_id=child["id"],
        visibility="entity",
        classification="internal",
        name="nested.md",
    )
    moved = await client.post(
        f"/api/v1/documents/folders/{moving['id']}/move",
        headers=headers,
        json={"parent_id": locked["id"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["classification"] == "confidential"
    assert moved.json()["visibility"] == "workspace"
    refreshed = await client.get(f"/api/v1/documents/{doc['id']}", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["classification"] == "confidential"
    assert refreshed.json()["visibility"] == "workspace"


@pytest.mark.asyncio
async def test_document_policy_updates_cannot_undercut_parent_folder(client: AsyncClient):
    headers = await _auth(client, "docparentpolicy")
    folder = await _create_folder(client, headers, "Confidential Workspace")
    await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=headers,
        json={"visibility": "workspace", "classification": "confidential", "cascade": False},
    )
    doc = await _upload(client, headers, folder_id=folder["id"], name="policy.md")
    lower = await client.post(
        f"/api/v1/permissions/documents/{doc['id']}/classify",
        headers=headers,
        json={"classification": "internal"},
    )
    assert lower.status_code == 409
    broaden = await client.post(
        f"/api/v1/permissions/documents/{doc['id']}/visibility",
        headers=headers,
        json={"visibility": "entity"},
    )
    assert broaden.status_code == 409

    open_doc = await _upload(client, headers, name="client-visible.md")
    visible = await client.post(
        f"/api/v1/permissions/documents/{open_doc['id']}/client-visible",
        headers=headers,
        json={"client_visible": True},
    )
    assert visible.status_code == 200, visible.text
    classified = await client.post(
        f"/api/v1/permissions/documents/{open_doc['id']}/classify",
        headers=headers,
        json={"classification": "confidential"},
    )
    assert classified.status_code == 200, classified.text
    refreshed = await client.get(f"/api/v1/documents/{open_doc['id']}", headers=headers)
    assert refreshed.json()["client_visible"] is False


# ── Folder grants ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_folder_grants_crud(client: AsyncClient):
    headers = await _auth(client, "foldgrant")
    _member_headers, member = await _invite_and_accept_member(
        client,
        headers,
        "foldgrant.member@test.com",
        name="Folder Grant Member",
    )
    folder = await _create_folder(client, headers, "Shared")
    # Create
    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": member["staff_id"],
            "capabilities": ["view", "comment"],
        },
    )
    assert resp.status_code == 201, resp.text
    grant = resp.json()
    assert set(grant["capabilities"]) == {"view", "comment"}
    assert grant["resource_type"] == "document_folder"
    assert grant["subject_id"] == member["user_id"]
    assert grant["subject_user_id"] == member["user_id"]
    assert grant["subject_staff_id"] == member["staff_id"]
    assert grant["subject_display_name"] == "Folder Grant Member"

    # List
    rows = (await client.get(f"/api/v1/folders/{folder['id']}/grants", headers=headers)).json()
    assert len(rows) == 1
    assert rows[0]["subject_email"] == "foldgrant.member@test.com"

    # Revoke
    revoke = await client.delete(
        f"/api/v1/folders/{folder['id']}/grants/{grant['id']}",
        headers=headers,
    )
    assert revoke.status_code == 204


@pytest.mark.asyncio
async def test_delegated_folder_grants_are_subset_bounded(client: AsyncClient):
    headers = await _auth(client, "folderdelegateowner")
    delegator_headers, delegator = await _invite_and_accept_member(
        client,
        headers,
        "folder.delegate@test.com",
        name="Folder Delegator",
    )
    _recipient_headers, recipient = await _invite_and_accept_member(
        client,
        headers,
        "folder.recipient@test.com",
        name="Folder Recipient",
    )
    folder = await _create_folder(client, headers, "Delegated Folder")

    delegated = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": delegator["user_id"],
            "capabilities": ["view", "upload_to", "share_internal"],
        },
    )
    assert delegated.status_code == 201, delegated.text
    hidden_acl = await client.get(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=delegator_headers,
    )
    assert hidden_acl.status_code == 403

    allowed = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=delegator_headers,
        json={
            "subject_type": "user",
            "subject_id": recipient["user_id"],
            "capabilities": ["view"],
        },
    )
    assert allowed.status_code == 201, allowed.text
    escalated = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=delegator_headers,
        json={
            "subject_type": "user",
            "subject_id": recipient["user_id"],
            "capabilities": ["view", "delete"],
        },
    )
    assert escalated.status_code == 403
    external = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=delegator_headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert external.status_code == 403


@pytest.mark.asyncio
async def test_folder_grant_rejects_unsupported_subject_type(client: AsyncClient):
    headers = await _auth(client, "foldgrantsubject")
    folder = await _create_folder(client, headers, "Only Users")
    response = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=headers,
        json={
            "subject_type": "workspace_role",
            "subject_id": "viewer",
            "capabilities": ["view"],
        },
    )
    assert response.status_code == 400


# ── Folder shares ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_folder_share_create_revoke(client: AsyncClient):
    headers = await _auth(client, "foldshare")
    folder = await _create_folder(client, headers, "Public")
    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": 7,
        },
    )
    assert resp.status_code == 201, resp.text
    share = resp.json()
    assert share["token"]
    assert share["url"].endswith(share["token"])
    assert share["audience"] == "anonymous"

    # Confidential folder refuses external share with 409
    cf = await _create_folder(client, headers, "Conf")
    await client.post(
        f"/api/v1/folders/{cf['id']}/properties",
        headers=headers,
        json={"classification": "confidential", "cascade": False},
    )
    resp = await client.post(
        f"/api/v1/folders/{cf['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_folder_share_can_never_expire(client: AsyncClient):
    headers = await _auth(client, "permanentfolder")
    folder = await _create_folder(client, headers, "Permanent")
    created = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": None,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is None

    public = await client.get(f"/api/v1/shared-folder/{created.json()['token']}")
    assert public.status_code == 200, public.text
    assert public.json()["expires_at"] is None


@pytest.mark.asyncio
async def test_deleted_workspace_blocks_folder_acl_and_public_share_until_restore(
    client: AsyncClient,
):
    headers = await _auth(client, "deletedworkspacefoldershare")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restorable folder permissions"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    folder_id = workspace_data["artifact_folder_id"]
    assert folder_id

    share = await client.post(
        f"/api/v1/folders/{folder_id}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]
    assert (await client.get(f"/api/v1/shared-folder/{token}")).status_code == 200

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_data['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/api/v1/shared-folder/{token}")).status_code == 410
    assert (
        await client.get(
            f"/api/v1/folders/{folder_id}/grants",
            headers=headers,
        )
    ).status_code == 404

    restored = await client.post(
        f"/api/v1/workspaces/{workspace_data['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"/api/v1/shared-folder/{token}")).status_code == 200


@pytest.mark.asyncio
async def test_deleted_workspace_blocks_knowledge_folder_crud_until_restore(
    client: AsyncClient,
):
    headers = await _auth(client, "deletedworkspacefoldercrud")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restorable folder tree"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    child = await _create_folder(
        client,
        headers,
        "Preserved child",
        parent_id=workspace_data["artifact_folder_id"],
    )
    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_data['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    create = await client.post(
        "/api/v1/documents/folders",
        headers=headers,
        json={"name": "Must not be created", "parent_id": child["id"]},
    )
    rename = await client.put(
        f"/api/v1/documents/folders/{child['id']}",
        headers=headers,
        json={"name": "Must not be renamed"},
    )
    move = await client.post(
        f"/api/v1/documents/folders/{child['id']}/move",
        headers=headers,
        json={"parent_id": None},
    )
    remove = await client.delete(
        f"/api/v1/documents/folders/{child['id']}",
        headers=headers,
    )
    assert {create.status_code, rename.status_code, move.status_code, remove.status_code} == {404}

    restored = await client.post(
        f"/api/v1/workspaces/{workspace_data['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    tree = await client.get("/api/v1/documents/folder-tree", headers=headers)
    assert tree.status_code == 200, tree.text
    assert child["id"] in {folder["id"] for folder in tree.json()}


@pytest.mark.asyncio
async def test_folder_rename_serializes_with_workspace_delete(
    client: AsyncClient,
):
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "folderrenamedeleterace")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Folder rename delete race"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    child = await _create_folder(
        client,
        headers,
        "Stable child name",
        parent_id=workspace_data["artifact_folder_id"],
    )

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_data["id"],
            workspace_data["entity_id"],
        ) is True
        rename_task = asyncio.create_task(client.put(
            f"/api/v1/documents/folders/{child['id']}",
            headers=headers,
            json={"name": "Must not win"},
        ))
        await asyncio.sleep(0.1)
        assert rename_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(rename_task, timeout=5)
    assert response.status_code == 404, response.text
    restored = await client.post(
        f"/api/v1/workspaces/{workspace_data['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    tree = await client.get("/api/v1/documents/folder-tree", headers=headers)
    assert tree.status_code == 200, tree.text
    child_after_restore = next(
        folder for folder in tree.json() if folder["id"] == child["id"]
    )
    assert child_after_restore["name"] == "Stable child name"


@pytest.mark.asyncio
async def test_public_folder_share_serializes_with_workspace_delete(
    client: AsyncClient,
):
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "publicfolderdeleterace")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Public folder delete race"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    folder_id = workspace_data["artifact_folder_id"]
    share = await client.post(
        f"/api/v1/folders/{folder_id}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_data["id"],
            workspace_data["entity_id"],
        ) is True
        request_task = asyncio.create_task(
            client.get(f"/api/v1/shared-folder/{token}")
        )
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 410, response.text


@pytest.mark.asyncio
async def test_public_folder_share_serializes_with_direct_child_workspace_delete(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.document import Document
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "publicfolderchilddeleterace")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    entity_id = me.json()["entity_id"]
    parent = await _create_folder(client, headers, "Shared parent")
    visible = await _upload(
        client,
        headers,
        folder_id=parent["id"],
        name="visible.md",
    )
    hidden = await _upload(
        client,
        headers,
        folder_id=parent["id"],
        name="hidden.md",
    )
    hidden_folder = await _create_folder(
        client,
        headers,
        "Child Workspace",
        parent_id=parent["id"],
    )
    child_workspace = Workspace(
        entity_id=entity_id,
        name="Direct child owner",
        artifact_folder_id=hidden_folder["id"],
        status="active",
    )
    db_session.add(child_workspace)
    await db_session.flush()
    hidden_document = await db_session.get(Document, hidden["id"])
    assert hidden_document is not None
    hidden_document.metadata_ = {
        "origin": {"workspace_id": child_workspace.id}
    }
    await db_session.commit()

    share = await client.post(
        f"/api/v1/folders/{parent['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            child_workspace.id,
            entity_id,
        ) is True
        request_task = asyncio.create_task(
            client.get(f"/api/v1/shared-folder/{token}")
        )
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 200, response.text
    assert [doc["id"] for doc in response.json()["documents"]] == [visible["id"]]
    assert response.json()["subfolders"] == []


# ── Cross-entity isolation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_folder_share_public_viewer(client: AsyncClient):
    """Anonymous /shared-folder/{token} returns folder contents + bumps use_count."""
    headers = await _auth(client, "foldpub")
    folder = await _create_folder(client, headers, "Public Folder")
    # Add a couple docs + a subfolder so the viewer has content to show.
    await _upload(client, headers, folder_id=folder["id"], name="doc1.md")
    await _upload(client, headers, folder_id=folder["id"], name="doc2.md")
    await _create_folder(client, headers, "Subdir", parent_id=folder["id"])

    share_resp = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    token = share_resp.json()["token"]

    # Public viewer (no auth)
    pub = await client.get(f"/api/v1/shared-folder/{token}")
    assert pub.status_code == 200, pub.text
    body = pub.json()
    assert body["name"] == "Public Folder"
    assert len(body["documents"]) == 2
    assert len(body["subfolders"]) == 1
    assert set(body["capabilities"]) == {"view"}

    # Second access bumps use_count
    shares_after = (await client.get(f"/api/v1/folders/{folder['id']}/shares", headers=headers)).json()
    assert shares_after[0]["use_count"] == 1


@pytest.mark.asyncio
async def test_shared_folder_otp_releases_database_locks_before_email_provider(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _auth(client, "folderotplockrelease")
    folder = await _create_folder(client, headers, "OTP lock release")
    share = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "recipient@example.com",
            "capabilities": ["view"],
            "require_otp": True,
        },
    )
    assert share.status_code == 201, share.text
    share_data = share.json()

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def delayed_email(_to: str, _code: str) -> bool:
        provider_started.set()
        await release_provider.wait()
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_share_verification_email",
        delayed_email,
    )
    otp_request = asyncio.create_task(client.post(
        f"/api/v1/shared-folder/{share_data['token']}/request-otp",
        json={"email": "recipient@example.com"},
    ))
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    try:
        revoked = await asyncio.wait_for(
            client.delete(
                f"/api/v1/folders/{folder['id']}/shares/{share_data['id']}",
                headers=headers,
            ),
            timeout=5,
        )
        assert revoked.status_code == 204, revoked.text
    finally:
        release_provider.set()

    sent = await asyncio.wait_for(otp_request, timeout=5)
    assert sent.status_code == 200, sent.text


@pytest.mark.asyncio
async def test_failed_shared_folder_otp_delivery_discards_undelivered_challenge(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.models.permission import Share

    headers = await _auth(client, "folderotpdeliveryfailure")
    folder = await _create_folder(client, headers, "OTP delivery failure")
    share = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "recipient@example.com",
            "capabilities": ["view"],
            "require_otp": True,
        },
    )
    assert share.status_code == 201, share.text

    async def fail_delivery(_to: str, _code: str) -> bool:
        return False

    monkeypatch.setattr(
        "packages.core.services.email_service.send_share_verification_email",
        fail_delivery,
    )
    response = await client.post(
        f"/api/v1/shared-folder/{share.json()['token']}/request-otp",
        json={"email": "recipient@example.com"},
    )
    assert response.status_code == 503, response.text

    share_row = await db_session.get(Share, share.json()["id"])
    assert share_row is not None
    await db_session.refresh(share_row)
    assert (share_row.metadata_ or {}).get("otp_challenges") == {}


@pytest.mark.asyncio
async def test_folder_share_max_uses_is_consumed_atomically(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import folder_permissions
    from packages.core.models.permission import Share

    headers = await _auth(client, "foldermaxusesrace")
    folder = await _create_folder(client, headers, "One use only")
    share = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text
    share_row = await db_session.get(Share, share.json()["id"])
    assert share_row is not None
    share_row.max_uses = 1
    await db_session.commit()

    original_effective_policy = folder_permissions.effective_folder_policy
    policy_entries = 0
    release_policy = asyncio.Event()

    async def synchronize_policy_reads(*args, **kwargs):
        nonlocal policy_entries
        policy_entries += 1
        if policy_entries >= 2:
            release_policy.set()
        try:
            await asyncio.wait_for(release_policy.wait(), timeout=0.25)
        except TimeoutError:
            pass
        return await original_effective_policy(*args, **kwargs)

    monkeypatch.setattr(
        folder_permissions,
        "effective_folder_policy",
        synchronize_policy_reads,
    )
    token = share.json()["token"]
    first, second = await asyncio.gather(
        client.get(f"/api/v1/shared-folder/{token}"),
        client.get(f"/api/v1/shared-folder/{token}"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 410]

    await db_session.refresh(share_row)
    assert share_row.use_count == 1


@pytest.mark.asyncio
async def test_folder_share_filters_confidential_children(client: AsyncClient):
    headers = await _auth(client, "foldpubfilter")
    folder = await _create_folder(client, headers, "External")
    await _upload(client, headers, folder_id=folder["id"], name="visible.md")
    await _upload(
        client,
        headers,
        folder_id=folder["id"],
        name="confidential.md",
        classification="confidential",
    )
    share = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    public = await client.get(f"/api/v1/shared-folder/{share.json()['token']}")
    assert public.status_code == 200, public.text
    assert [doc["name"] for doc in public.json()["documents"]] == ["visible.md"]


@pytest.mark.asyncio
async def test_folder_share_filters_children_owned_by_deleted_workspace(
    client: AsyncClient,
    db_session,
):
    from datetime import datetime, timezone

    from packages.core.models.document import Document
    from packages.core.models.workspace import Workspace

    headers = await _auth(client, "foldpubdeletedchildren")
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    entity_id = me.json()["entity_id"]
    folder = await _create_folder(client, headers, "Shared parent")
    visible = await _upload(
        client,
        headers,
        folder_id=folder["id"],
        name="visible.md",
    )
    hidden = await _upload(
        client,
        headers,
        folder_id=folder["id"],
        name="hidden.md",
    )
    hidden_folder = await _create_folder(
        client,
        headers,
        "Deleted Workspace child",
        parent_id=folder["id"],
    )

    workspace = Workspace(
        entity_id=entity_id,
        name="Deleted child ownership",
        artifact_folder_id=hidden_folder["id"],
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add(workspace)
    await db_session.flush()
    hidden_document = await db_session.get(Document, hidden["id"])
    assert hidden_document is not None
    hidden_document.metadata_ = {"origin": {"workspace_id": workspace.id}}
    await db_session.commit()

    share = await client.post(
        f"/api/v1/folders/{folder['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    public = await client.get(f"/api/v1/shared-folder/{token}")
    assert public.status_code == 200, public.text
    assert [document["id"] for document in public.json()["documents"]] == [
        visible["id"]
    ]
    assert public.json()["subfolders"] == []

    workspace.deleted_at = None
    await db_session.commit()
    restored = await client.get(f"/api/v1/shared-folder/{token}")
    assert restored.status_code == 200, restored.text
    assert {document["id"] for document in restored.json()["documents"]} == {
        visible["id"],
        hidden["id"],
    }
    assert [child["id"] for child in restored.json()["subfolders"]] == [
        hidden_folder["id"]
    ]


@pytest.mark.asyncio
async def test_folder_share_revalidates_ancestor_classification(client: AsyncClient, db_session):
    from packages.core.models.document import DocumentFolder

    headers = await _auth(client, "foldancestorpolicy")
    parent = await _create_folder(client, headers, "Protected Parent")
    child = await _create_folder(
        client,
        headers,
        "Legacy Internal Child",
        parent_id=parent["id"],
    )
    share = await client.post(
        f"/api/v1/folders/{child['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text

    # Simulate a pre-invariant tree whose child row was not normalized when an
    # ancestor became confidential. Read-time policy must still fail closed.
    parent_row = await db_session.get(DocumentFolder, parent["id"])
    parent_row.classification = "confidential"
    await db_session.commit()

    public = await client.get(f"/api/v1/shared-folder/{share.json()['token']}")
    assert public.status_code == 410, public.text
    replacement = await client.post(
        f"/api/v1/folders/{child['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert replacement.status_code == 409, replacement.text


@pytest.mark.asyncio
async def test_folder_share_public_viewer_expired(client: AsyncClient):
    """Revoked / nonexistent token returns 404."""
    pub = await client.get("/api/v1/shared-folder/totally-bogus-token")
    assert pub.status_code == 404


@pytest.mark.asyncio
async def test_foreign_entity_cannot_touch_folder(client: AsyncClient):
    a = await _auth(client, "foldera")
    b = await _auth(client, "folderb")
    folder = await _create_folder(client, a, "Mine")

    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/properties",
        headers=b,
        json={"classification": "confidential"},
    )
    assert resp.status_code == 404
    resp = await client.get(f"/api/v1/folders/{folder['id']}/grants", headers=b)
    assert resp.status_code == 404
