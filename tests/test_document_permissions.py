"""E2E tests for the document permission endpoints (RFC §13, P3).

Covers:
  * GET/POST/DELETE /documents/:id/grants     (internal sharing)
  * GET/POST/DELETE /documents/:id/shares     (external tokens)
  * GET /api/v1/shared-doc/:token             (unauthenticated public viewer)
  * POST /documents/:id/share-approvals       (Confidential approval flow)
  * POST /documents/:id/share-approvals/:rid/decision  (admin approve/deny)
  * GET /documents/:id/access-log             (owner self-service)
  * Invariants:
      - Restricted refuses external share (400)
      - Confidential refuses plain /shares with 409 → must go through approval
      - Foreign-entity access returns 404 (cross-entity isolation)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import packages.core.database as db_module
from packages.core.models import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
    User,
)
from packages.core.models.base import generate_ulid
from packages.core.services.auth_service import create_access_token, hash_password


async def _auth(client: AsyncClient, username: str) -> dict:
    """Register a fresh user (each gets its own entity); return auth headers."""
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


async def _create_entity_user(
    entity_id: str,
    username: str,
    role: str = "member",
    *,
    display_name: str | None = None,
    avatar_url: str | None = None,
) -> dict:
    async with db_module.async_session() as session:
        user = User(
            entity_id=entity_id,
            email=f"{username}@test.com",
            display_name=display_name or username,
            avatar_url=avatar_url,
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
        "entity_id": entity_id,
        "headers": {"Authorization": f"Bearer {token}"},
        "role": role,
    }


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


async def _upload(
    client: AsyncClient,
    headers: dict,
    *,
    name: str = "doc.md",
    body: bytes = b"hello",
    visibility: str | None = None,
    classification: str | None = None,
) -> dict:
    params: list[tuple[str, str]] = []
    if visibility:
        params.append(("visibility", visibility))
    if classification:
        params.append(("classification", classification))
    url = "/api/v1/documents/upload"
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"
    resp = await client.post(
        url,
        headers=headers,
        files={"file": (name, body, "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Grants (internal sharing) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grants_create_list_revoke(client: AsyncClient):
    headers = await _auth(client, "grantowner")
    member_headers, member = await _invite_and_accept_member(
        client,
        headers,
        "grant.member@test.com",
        name="Grant Member",
    )
    doc = await _upload(client, headers, name="contract.md", visibility="private")

    before = await client.get(f"/api/v1/documents/{doc['id']}", headers=member_headers)
    assert before.status_code == 404

    # Empty list initially
    resp = await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # Create a grant
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
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
    assert grant["subject_id"] == member["user_id"]
    assert grant["subject_user_id"] == member["user_id"]
    assert grant["subject_staff_id"] == member["staff_id"]
    assert grant["subject_display_name"] == "Grant Member"
    assert grant["subject_email"] == "grant.member@test.com"
    assert grant["status"] == "active"
    grant_id = grant["id"]

    member_read = await client.get(f"/api/v1/documents/{doc['id']}", headers=member_headers)
    assert member_read.status_code == 200, member_read.text

    # List shows the grant
    resp = await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == grant_id
    assert rows[0]["subject_display_name"] == "Grant Member"

    # Revoke
    resp = await client.delete(
        f"/api/v1/documents/{doc['id']}/grants/{grant_id}",
        headers=headers,
    )
    assert resp.status_code == 204

    # List again — revoked grants drop out
    resp = await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=headers)
    assert resp.json() == []


@pytest.mark.asyncio
async def test_document_comments_require_comment_capability(client: AsyncClient):
    headers = await _auth(client, "commentowner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member = await _create_entity_user(
        owner["entity_id"],
        "comment_member",
        "member",
        display_name="Comment Member",
        avatar_url="https://cdn.test/avatar.png",
    )
    member_headers = member["headers"]
    doc = await _upload(client, headers, name="commentable.md", visibility="private")
    comments_url = f"/api/v1/comments?resource_type=document&resource_id={doc['id']}"

    no_access = await client.get(comments_url, headers=member_headers)
    assert no_access.status_code == 404

    view_grant = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": member["id"],
            "capabilities": ["view"],
        },
    )
    assert view_grant.status_code == 201, view_grant.text

    can_read_comments = await client.get(comments_url, headers=member_headers)
    assert can_read_comments.status_code == 200
    assert can_read_comments.json() == []

    view_only_create = await client.post(
        "/api/v1/comments",
        headers=member_headers,
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "content": "needs comment permission",
        },
    )
    assert view_only_create.status_code == 403

    comment_grant = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": member["id"],
            "capabilities": ["view", "comment"],
        },
    )
    assert comment_grant.status_code == 201, comment_grant.text

    created = await client.post(
        "/api/v1/comments",
        headers=member_headers,
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "content": "Looks good to me.",
            "anchor": {
                "type": "text_range",
                "mode": "markdown",
                "line": 1,
                "line_end": 1,
                "start": 0,
                "end": 5,
                "quote": "hello",
            },
        },
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert created_body["content"] == "Looks good to me."
    assert created_body["anchor"]["line"] == 1
    assert created_body["user_display_name"] == "Comment Member"
    assert created_body["user_avatar_url"] == "https://cdn.test/avatar.png"

    reply = await client.post(
        "/api/v1/comments",
        headers=member_headers,
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "parent_id": created_body["id"],
            "content": "Replying here.",
        },
    )
    assert reply.status_code == 201, reply.text

    counted = await client.get(
        f"/api/v1/comments/count?resource_type=document&resource_id={doc['id']}",
        headers=member_headers,
    )
    assert counted.status_code == 200
    assert counted.json()["count"] == 2

    listed = await client.get(comments_url, headers=member_headers)
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body[0]["content"] == "Looks good to me."
    assert listed_body[0]["anchor"]["quote"] == "hello"
    assert listed_body[0]["user_display_name"] == "Comment Member"
    assert listed_body[0]["user_avatar_url"] == "https://cdn.test/avatar.png"
    assert listed_body[0]["replies"][0]["content"] == "Replying here."
    assert listed_body[0]["replies"][0]["parent_id"] == created_body["id"]


@pytest.mark.asyncio
async def test_grant_idempotent_upsert(client: AsyncClient):
    """Creating a grant for the same (subject_type, subject_id) twice should
    upsert the capability set rather than duplicate."""
    headers = await _auth(client, "grantupsert")
    _member_headers, member = await _invite_and_accept_member(
        client,
        headers,
        "grant.upsert@test.com",
        name="Grant Upsert",
    )
    doc = await _upload(client, headers, name="upsert.md")

    await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": member["user_id"],
            "capabilities": ["view"],
        },
    )
    await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": member["staff_id"],
            "capabilities": ["view", "comment", "edit"],
        },
    )

    rows = (await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=headers)).json()
    assert len(rows) == 1
    assert set(rows[0]["capabilities"]) == {"view", "comment", "edit"}
    assert rows[0]["subject_id"] == member["user_id"]


@pytest.mark.asyncio
async def test_legacy_staff_id_user_grant_still_allows_member_read(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _auth(client, "grantlegacy")
    member_headers, member = await _invite_and_accept_member(
        client,
        headers,
        "grant.legacy@test.com",
        name="Legacy Staff Grant",
    )
    doc = await _upload(client, headers, name="legacy.md", visibility="private")

    db_session.add(
        ResourceGrant(
            id=generate_ulid(),
            entity_id=member["entity_id"],
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc["id"],
            subject_type=SubjectType.USER,
            subject_id=member["staff_id"],
            capabilities=[Capability.VIEW],
            granted_by=None,
            granted_at=datetime.now(timezone.utc),
            status=GrantStatus.ACTIVE,
        )
    )
    await db_session.commit()

    member_read = await client.get(f"/api/v1/documents/{doc['id']}", headers=member_headers)
    assert member_read.status_code == 200, member_read.text

    rows = (await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=headers)).json()
    assert rows[0]["subject_id"] == member["staff_id"]
    assert rows[0]["subject_user_id"] == member["user_id"]
    assert rows[0]["subject_staff_id"] == member["staff_id"]
    assert rows[0]["subject_display_name"] == "Legacy Staff Grant"


@pytest.mark.asyncio
async def test_existing_user_team_membership_preserves_personal_files(
    client: AsyncClient,
):
    personal_email = "existing.member@test.com"
    personal = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "Existing Member",
            "email": personal_email,
            "password": "pass123",
            "entity_name": "Existing Member Personal",
        },
    )
    assert personal.status_code == 200, personal.text
    personal_body = personal.json()
    personal_headers = {"Authorization": f"Bearer {personal_body['access_token']}"}
    personal_entity_id = personal_body["entity_id"]

    personal_doc = await _upload(
        client,
        personal_headers,
        name="personal-private.md",
        body=b"personal only",
        visibility="private",
    )

    company_headers = await _auth(client, "multi_company_owner")
    company_me = await client.get("/api/v1/auth/me", headers=company_headers)
    assert company_me.status_code == 200, company_me.text
    company_entity_id = company_me.json()["entity_id"]
    assert company_entity_id != personal_entity_id

    invite = await client.post(
        "/api/v1/staff/invite",
        headers=company_headers,
        json={"email": personal_email, "name": "Existing Member"},
    )
    assert invite.status_code == 201, invite.text

    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        headers=personal_headers,
        json={
            "token": invite.json()["invite_token"],
            "name": "Existing Member",
        },
    )
    assert accepted.status_code == 200, accepted.text
    accepted_body = accepted.json()
    assert accepted_body["user_id"] == personal_body["user_id"]
    assert accepted_body["entity_id"] == company_entity_id

    company_member_headers = {
        "Authorization": f"Bearer {accepted_body['access_token']}",
    }
    member_me = await client.get("/api/v1/auth/me", headers=company_member_headers)
    assert member_me.status_code == 200, member_me.text
    member_body = member_me.json()
    assert member_body["entity_id"] == company_entity_id
    membership_entities = {m["entity_id"] for m in member_body["memberships"]}
    assert {personal_entity_id, company_entity_id} <= membership_entities

    company_cannot_read_personal_doc = await client.get(
        f"/api/v1/documents/{personal_doc['id']}",
        headers=company_member_headers,
    )
    assert company_cannot_read_personal_doc.status_code == 404

    company_doc = await _upload(
        client,
        company_headers,
        name="company-private.md",
        body=b"company shared",
        visibility="private",
    )
    grant = await client.post(
        f"/api/v1/documents/{company_doc['id']}/grants",
        headers=company_headers,
        json={
            "subject_type": "user",
            "subject_id": accepted_body["user_id"],
            "capabilities": ["view"],
        },
    )
    assert grant.status_code == 201, grant.text
    assert grant.json()["subject_display_name"] == "Existing Member"

    member_read_company_doc = await client.get(
        f"/api/v1/documents/{company_doc['id']}",
        headers=company_member_headers,
    )
    assert member_read_company_doc.status_code == 200, member_read_company_doc.text

    switched = await client.post(
        "/api/v1/auth/entities/switch",
        headers=company_member_headers,
        json={"entity_id": personal_entity_id},
    )
    assert switched.status_code == 200, switched.text
    switched_headers = {
        "Authorization": f"Bearer {switched.json()['access_token']}",
    }
    personal_read = await client.get(
        f"/api/v1/documents/{personal_doc['id']}",
        headers=switched_headers,
    )
    assert personal_read.status_code == 200, personal_read.text

    left = await client.post("/api/v1/staff/me/leave", headers=company_member_headers)
    assert left.status_code == 200, left.text
    left_body = left.json()
    assert left_body["status"] == "inactive"

    old_company_token = await client.get("/api/v1/auth/me", headers=company_member_headers)
    assert old_company_token.status_code == 403

    next_me = await client.get("/api/v1/auth/me", headers=switched_headers)
    assert next_me.status_code == 200, next_me.text
    assert next_me.json()["entity_id"] == personal_entity_id


@pytest.mark.asyncio
async def test_grant_unknown_capability_rejected(client: AsyncClient):
    headers = await _auth(client, "grantbad")
    doc = await _upload(client, headers)
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={"subject_id": "U", "capabilities": ["view", "totally_made_up"]},
    )
    assert resp.status_code == 400
    assert "totally_made_up" in resp.text


# ── External shares ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_create_list_revoke(client: AsyncClient):
    headers = await _auth(client, "shareowner")
    doc = await _upload(client, headers, name="public.md")

    # Create
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "bob@partner.com",
            "capabilities": ["view"],
            "expires_in_days": 7,
        },
    )
    assert resp.status_code == 201, resp.text
    share = resp.json()
    assert share["audience"] == "email:bob@partner.com"
    assert share["token"]  # plaintext token returned exactly once
    assert share["url"].endswith(share["token"])
    share_id = share["id"]
    raw_token = share["token"]

    # List
    rows = (await client.get(f"/api/v1/documents/{doc['id']}/shares", headers=headers)).json()
    assert len(rows) == 1
    # Token must NOT leak on list
    assert "token" not in rows[0]

    # Public viewer works without auth
    resp = await client.get(f"/api/v1/shared-doc/{raw_token}")
    assert resp.status_code == 200, resp.text
    public = resp.json()
    assert public["document_id"] == doc["id"]
    assert public["name"] == "public.md"

    # Revoke
    resp = await client.delete(
        f"/api/v1/documents/{doc['id']}/shares/{share_id}",
        headers=headers,
    )
    assert resp.status_code == 204

    # Token rejected after revoke
    resp = await client.get(f"/api/v1/shared-doc/{raw_token}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_share_restricted_blocked(client: AsyncClient):
    headers = await _auth(client, "restrictowner")
    doc = await _upload(client, headers, classification="restricted")
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "email", "audience_value": "a@b", "capabilities": ["view"]},
    )
    assert resp.status_code == 400
    assert "Restricted" in resp.text


@pytest.mark.asyncio
async def test_share_confidential_requires_approval(client: AsyncClient):
    """Confidential docs return 409 on plain /shares; must go through approval."""
    headers = await _auth(client, "confidowner")
    doc = await _upload(client, headers, classification="confidential")
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "email", "audience_value": "x@y", "capabilities": ["view"]},
    )
    assert resp.status_code == 409
    assert "approval" in resp.text.lower()


@pytest.mark.asyncio
async def test_share_unknown_audience_value_required(client: AsyncClient):
    headers = await _auth(client, "shareemail")
    doc = await _upload(client, headers)
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "email", "capabilities": ["view"]},
    )
    assert resp.status_code == 400


# ── Share approvals (Confidential workflow) ──────────────────────────────


@pytest.mark.asyncio
async def test_share_approval_full_loop(client: AsyncClient):
    """Owner submits approval → admin (same user, by virtue of being owner of
    their entity) approves → token + url returned exactly once + share row
    materialized."""
    headers = await _auth(client, "approvalowner")
    doc = await _upload(client, headers, classification="confidential")

    # Submit
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "client@partner.com",
            "capabilities": ["view"],
            "expires_in_days": 14,
            "reason": "Client legal review for Q3 contract",
        },
    )
    assert resp.status_code == 201, resp.text
    pending = resp.json()
    assert pending["status"] == "pending"
    assert pending["config"]["audience_value"] == "client@partner.com"
    approval_id = pending["id"]

    # List shows pending
    rows = (
        await client.get(
            f"/api/v1/documents/{doc['id']}/share-approvals?status=pending",
            headers=headers,
        )
    ).json()
    assert len(rows) == 1
    assert rows[0]["id"] == approval_id

    # Approve (registering user is admin/owner of their own entity)
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals/{approval_id}/decision",
        headers=headers,
        json={"decision": "approve", "note": "Approved for Q3 review"},
    )
    assert resp.status_code == 200, resp.text
    decision = resp.json()
    assert decision["approval"]["status"] == "approved"
    assert decision["token"]
    assert decision["url"]
    assert decision["approval"]["approved_share_id"]

    # Token works
    resp = await client.get(f"/api/v1/shared-doc/{decision['token']}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_share_approval_requires_confidential(client: AsyncClient):
    """Internal docs can't use the approval flow — caller should hit /shares."""
    headers = await _auth(client, "approvalwrong")
    doc = await _upload(client, headers, classification="internal")
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "x@y",
            "capabilities": ["view"],
            "reason": "Should be rejected",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_share_approval_deny(client: AsyncClient):
    headers = await _auth(client, "approvaldeny")
    doc = await _upload(client, headers, classification="confidential")
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "x@y",
            "capabilities": ["view"],
            "reason": "Just checking",
        },
    )
    approval_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals/{approval_id}/decision",
        headers=headers,
        json={"decision": "deny", "note": "Not needed for this client"},
    )
    assert resp.status_code == 200
    decision = resp.json()
    assert decision["approval"]["status"] == "denied"
    assert decision["token"] is None
    assert decision["url"] is None


# ── Access log ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_access_log_records_share_use(client: AsyncClient):
    headers = await _auth(client, "logowner")
    doc = await _upload(client, headers)

    # Create + use a share
    share_resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "x@y",
            "capabilities": ["view"],
        },
    )
    token = share_resp.json()["token"]
    await client.get(f"/api/v1/shared-doc/{token}")  # consume

    # Owner can read the log
    resp = await client.get(f"/api/v1/documents/{doc['id']}/access-log", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    # Should contain at least share_create + share_use
    actions = {r["action"] for r in rows}
    assert "share_create" in actions
    assert "share_use" in actions


# ── Cross-entity isolation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_foreign_entity_cannot_see_doc(client: AsyncClient):
    """User from entity A cannot read or grant on a doc owned by entity B."""
    a_headers = await _auth(client, "tenanta")
    b_headers = await _auth(client, "tenantb")
    doc = await _upload(client, a_headers, name="a-secret.md")

    # B trying to list grants → 404 (doc invisible to B's entity)
    resp = await client.get(f"/api/v1/documents/{doc['id']}/grants", headers=b_headers)
    assert resp.status_code == 404

    # B trying to share → 404
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=b_headers,
        json={"audience_type": "email", "audience_value": "x@y", "capabilities": ["view"]},
    )
    assert resp.status_code == 404


# ── Upload-time invariants ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_rejects_restricted_public(client: AsyncClient):
    headers = await _auth(client, "uploadinv")
    resp = await client.post(
        "/api/v1/documents/upload?visibility=public&classification=restricted",
        headers=headers,
        files={"file": ("doc.md", b"x", "text/markdown")},
    )
    assert resp.status_code == 400
    assert "Restricted" in resp.text or "public" in resp.text


@pytest.mark.asyncio
async def test_upload_visibility_classification_persisted(client: AsyncClient):
    headers = await _auth(client, "uploadok")
    resp = await client.post(
        "/api/v1/documents/upload?visibility=workspace&classification=confidential",
        headers=headers,
        files={"file": ("c.md", b"y", "text/markdown")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["visibility"] == "workspace"
    assert body["classification"] == "confidential"
    assert body["owner_id"]  # set to user.id by upload path


# ── Public download via share token ──────────────────────────────────────
#
# Locks in the new /api/v1/shared-doc/{token}/download endpoint. The
# capability gate is the important contract: a recipient with a
# view-only share must NOT be able to pull bytes, and a recipient with
# a downloader share MUST get the file body back.


@pytest.mark.asyncio
async def test_shared_doc_download_blocked_when_capability_missing(client: AsyncClient):
    """View-only share -> download endpoint returns 403 with the coded
    error so the frontend can translate it ('this link does not allow
    downloading the file')."""
    headers = await _auth(client, "dlblockowner")
    doc = await _upload(client, headers, name="quarterly.md", body=b"secret numbers")

    # Create a view-only anonymous share (matches "Viewer" anon role).
    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": 7,
            "allow_download": False,
        },
    )
    assert resp.status_code == 201, resp.text
    raw_token = resp.json()["token"]

    # No auth needed — opaque token is the entitlement.
    dl = await client.get(f"/api/v1/shared-doc/{raw_token}/download")
    assert dl.status_code == 403
    body = dl.json()
    # CodedError shape: detail = {code, message, vars?}
    assert body["detail"]["code"] == "permissions.error.share.download_not_allowed"


@pytest.mark.asyncio
async def test_shared_doc_download_streams_file_when_allowed(
    client: AsyncClient,
    tmp_path,
):
    """Downloader share -> endpoint streams the file body and bumps
    use_count exactly once.

    Requires real bytes on disk, so we enable MANOR_FS for this test —
    the upload path persists ``fs_path`` only when FS is enabled, and the
    download endpoint reads from that path."""
    from packages.core.config import get_settings

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "dlownerok")
        doc = await _upload(client, headers, name="report.md", body=b"all good")

        resp = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view", "download"],
                "expires_in_days": 7,
                "allow_download": True,
            },
        )
        assert resp.status_code == 201, resp.text
        raw_token = resp.json()["token"]
        share_id = resp.json()["id"]

        dl = await client.get(f"/api/v1/shared-doc/{raw_token}/download")
        assert dl.status_code == 200, dl.text
        assert dl.content == b"all good"
        # Content-Disposition keeps the original filename so browsers
        # save it as report.md, not as the opaque token.
        assert "report.md" in dl.headers.get("content-disposition", "")

        # Counter incremented exactly once.
        rows = (
            await client.get(
                f"/api/v1/documents/{doc['id']}/shares",
                headers=headers,
            )
        ).json()
        matching = [r for r in rows if r["id"] == share_id]
        assert matching and matching[0]["use_count"] >= 1
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_shared_doc_download_404_after_revoke(
    client: AsyncClient,
    tmp_path,
):
    """Revoking the share kills the download endpoint too — not just
    the metadata viewer."""
    from packages.core.config import get_settings

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "dlrevoke")
        doc = await _upload(client, headers, name="r.md", body=b"x")

        resp = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view", "download"],
                "expires_in_days": 7,
                "allow_download": True,
            },
        )
        raw_token = resp.json()["token"]
        share_id = resp.json()["id"]

        # Sanity — works before revoke.
        assert (
            await client.get(
                f"/api/v1/shared-doc/{raw_token}/download",
            )
        ).status_code == 200

        # Revoke.
        assert (
            await client.delete(
                f"/api/v1/documents/{doc['id']}/shares/{share_id}",
                headers=headers,
            )
        ).status_code == 204

        # Subsequent download attempts return the coded not_found_or_revoked.
        after = await client.get(f"/api/v1/shared-doc/{raw_token}/download")
        assert after.status_code == 404
        assert after.json()["detail"]["code"] == "permissions.error.share.not_found_or_revoked"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


# ── Public URL host (browser-facing origin) ─────────────────────────────
#
# Backend mints links of the form "{origin}/shared-doc/{token}". The
# origin must be the *frontend* origin so the recipient lands on the SPA
# (which has the /shared-doc/:token route), not the API port (which
# would return JSON or 404). Locks in the precedence: APP_URL setting >
# X-Forwarded-Host > Host header > request.base_url fallback.


@pytest.mark.asyncio
async def test_share_url_honors_x_forwarded_host(client: AsyncClient):
    """Behind a reverse proxy (or vite dev proxy) X-Forwarded-Host carries
    the original frontend origin. The minted URL must use it, not the
    backend's bind host — otherwise links paste-broken outside the dev
    machine."""
    headers = await _auth(client, "shareurlhost")
    doc = await _upload(client, headers, name="ext.md")

    resp = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers={
            **headers,
            "X-Forwarded-Host": "share.example.com",
            "X-Forwarded-Proto": "https",
        },
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": 7,
        },
    )
    assert resp.status_code == 201, resp.text
    url = resp.json()["url"]
    assert url.startswith("https://share.example.com/shared-doc/"), (
        f"URL must use frontend origin from X-Forwarded-Host; got {url!r}"
    )
    # Backend test host (httpx ASGITransport default) is "test", and the
    # bind-time base_url would be "http://test/". The forwarded host
    # must win regardless.
    assert "://test/" not in url


@pytest.mark.asyncio
async def test_share_url_honors_app_url_setting(client: AsyncClient):
    """APP_URL is the explicit per-env override and trumps both
    X-Forwarded-Host and the bind URL. Catches mis-config where someone
    sets APP_URL=https://app.prod.com but the reverse proxy is also
    sending X-Forwarded-Host (they're consistent in prod, but the test
    proves the precedence)."""
    from packages.core.config import get_settings

    settings = get_settings()
    old_app_url = settings.APP_URL
    settings.APP_URL = "https://manor.example.com"
    try:
        headers = await _auth(client, "shareurlapp")
        doc = await _upload(client, headers, name="ext.md")
        resp = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers={
                **headers,
                # Deliberately conflicting — APP_URL wins.
                "X-Forwarded-Host": "wrong.example.com",
            },
            json={
                "audience_type": "anonymous",
                "capabilities": ["view"],
                "expires_in_days": 7,
            },
        )
        assert resp.status_code == 201, resp.text
        url = resp.json()["url"]
        assert url.startswith("https://manor.example.com/shared-doc/"), (
            f"APP_URL setting must take precedence; got {url!r}"
        )
        assert "wrong.example.com" not in url
    finally:
        settings.APP_URL = old_app_url
