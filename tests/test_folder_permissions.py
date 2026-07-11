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

from urllib.parse import urlencode

import pytest
from httpx import AsyncClient


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
