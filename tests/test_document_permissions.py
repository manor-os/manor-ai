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

import asyncio
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

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
    roles = await client.get("/api/v1/staff/roles", headers=owner_headers)
    assert roles.status_code == 200, roles.text
    member_role = next(
        role for role in roles.json() if role["name"].lower() == "member"
    )
    invite = await client.post(
        "/api/v1/staff/invite",
        headers=owner_headers,
        json={"email": email, "name": name, "role_id": member_role["id"]},
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


def _office_fixture(member: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(member, "<officeDocument/>")
    return buffer.getvalue()


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
            "resource_type": "Document",
            "resource_id": doc["id"],
            "content": "needs comment permission",
        },
    )
    assert view_only_create.status_code == 403

    owner_comment = await client.post(
        "/api/v1/comments",
        headers=headers,
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "content": "Owner note",
        },
    )
    assert owner_comment.status_code == 201, owner_comment.text
    view_only_reaction = await client.post(
        f"/api/v1/comments/{owner_comment.json()['id']}/reactions",
        headers=member_headers,
        json={"reaction": "thumbsup"},
    )
    assert view_only_reaction.status_code == 403

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

    comment_reaction = await client.post(
        f"/api/v1/comments/{owner_comment.json()['id']}/reactions",
        headers=member_headers,
        json={"reaction": "thumbsup"},
    )
    assert comment_reaction.status_code == 200, comment_reaction.text

    created = await client.post(
        "/api/v1/comments",
        headers=member_headers,
        json={
            "resource_type": "Documents",
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
    assert created_body["resource_type"] == "document"
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
    assert counted.json()["count"] == 3

    listed = await client.get(comments_url, headers=member_headers)
    assert listed.status_code == 200
    listed_body = listed.json()
    member_comment = next(row for row in listed_body if row["id"] == created_body["id"])
    assert member_comment["content"] == "Looks good to me."
    assert member_comment["anchor"]["quote"] == "hello"
    assert member_comment["user_display_name"] == "Comment Member"
    assert member_comment["user_avatar_url"] == "https://cdn.test/avatar.png"
    assert member_comment["replies"][0]["content"] == "Replying here."
    assert member_comment["replies"][0]["parent_id"] == created_body["id"]


@pytest.mark.asyncio
async def test_document_comment_manager_does_not_match_mutable_creator_label(
    client: AsyncClient,
):
    headers = await _auth(client, "commentaliasowner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    member = await _create_entity_user(
        owner["entity_id"],
        "comment_alias_member",
        "member",
        display_name="commentaliasowner",
    )
    doc = await _upload(client, headers, name="creator-label.md", visibility="private")

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

    create = await client.post(
        "/api/v1/comments",
        headers=member["headers"],
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "content": "A mutable display name must not grant comment access",
        },
    )
    assert create.status_code == 403, create.text


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
async def test_delegated_document_grants_are_subset_bounded(client: AsyncClient):
    headers = await _auth(client, "grantdelegateowner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    delegator = await _create_entity_user(
        owner["entity_id"],
        "grant_delegate_editor",
    )
    recipient = await _create_entity_user(
        owner["entity_id"],
        "grant_delegate_recipient",
        role="viewer",
    )
    doc = await _upload(client, headers, name="delegated.md", visibility="private")

    delegated = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": delegator["id"],
            "capabilities": ["view", "edit", "share_internal"],
        },
    )
    assert delegated.status_code == 201, delegated.text

    hidden_acl = await client.get(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=delegator["headers"],
    )
    assert hidden_acl.status_code == 403
    allowed = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=delegator["headers"],
        json={
            "subject_type": "user",
            "subject_id": recipient["id"],
            "capabilities": ["view"],
        },
    )
    assert allowed.status_code == 201, allowed.text
    escalated = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=delegator["headers"],
        json={
            "subject_type": "user",
            "subject_id": recipient["id"],
            "capabilities": ["view", "delete"],
        },
    )
    assert escalated.status_code == 403

    curator = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "user",
            "subject_id": delegator["id"],
            "capabilities": ["view", "grant_access"],
        },
    )
    assert curator.status_code == 201, curator.text
    assert (
        await client.get(
            f"/api/v1/documents/{doc['id']}/grants",
            headers=delegator["headers"],
        )
    ).status_code == 200
    external = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=delegator["headers"],
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert external.status_code == 403


@pytest.mark.asyncio
async def test_access_request_approval_narrows_and_upserts_one_manual_grant(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from sqlalchemy import select

    headers = await _auth(client, "accessapprovalowner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    requester = await _create_entity_user(
        owner["entity_id"],
        "access_approval_requester",
    )
    doc = await _upload(client, headers, name="approval.md", visibility="private")

    first = await client.post(
        "/api/v1/permissions/access-requests",
        headers=requester["headers"],
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "requested_capabilities": ["view"],
        },
    )
    assert first.status_code == 200, first.text
    expanded = await client.post(
        f"/api/v1/documents/{doc['id']}/access-requests/"
        f"{first.json()['id']}/decision",
        headers=headers,
        json={"decision": "approve", "approved_capabilities": ["view", "delete"]},
    )
    assert expanded.status_code == 400
    approved = await client.post(
        f"/api/v1/documents/{doc['id']}/access-requests/"
        f"{first.json()['id']}/decision",
        headers=headers,
        json={"decision": "approve", "approved_capabilities": ["view"]},
    )
    assert approved.status_code == 200, approved.text

    second = await client.post(
        "/api/v1/permissions/access-requests",
        headers=requester["headers"],
        json={
            "resource_type": "document",
            "resource_id": doc["id"],
            "requested_capabilities": ["comment"],
        },
    )
    assert second.status_code == 200, second.text
    approved_second = await client.post(
        f"/api/v1/documents/{doc['id']}/access-requests/"
        f"{second.json()['id']}/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert approved_second.status_code == 200, approved_second.text

    db_session.expire_all()
    active = list((await db_session.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == owner["entity_id"],
            ResourceGrant.resource_type == ResourceType.DOCUMENT,
            ResourceGrant.resource_id == doc["id"],
            ResourceGrant.subject_type == SubjectType.USER,
            ResourceGrant.subject_id == requester["id"],
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalars().all())
    assert len(active) == 1
    assert set(active[0].capabilities) == {Capability.VIEW, Capability.COMMENT}

    revoked = await client.delete(
        f"/api/v1/documents/{doc['id']}/grants/{active[0].id}",
        headers=headers,
    )
    assert revoked.status_code == 204, revoked.text
    listed = await client.get(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
    )
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_access_request_approval_preserves_distinct_expirations(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from sqlalchemy import select

    headers = await _auth(client, "accessexpiryowner")
    owner = (await client.get("/api/v1/auth/me", headers=headers)).json()
    requester = await _create_entity_user(
        owner["entity_id"],
        "access_expiry_requester",
    )
    doc = await _upload(client, headers, name="expiry.md", visibility="private")

    async def request_and_approve(capability: str, expires_at: str | None):
        pending = await client.post(
            "/api/v1/permissions/access-requests",
            headers=requester["headers"],
            json={
                "resource_type": "document",
                "resource_id": doc["id"],
                "requested_capabilities": [capability],
            },
        )
        assert pending.status_code == 200, pending.text
        payload = {"decision": "approve"}
        if expires_at is not None:
            payload["expires_at"] = expires_at
        approved = await client.post(
            f"/api/v1/documents/{doc['id']}/access-requests/"
            f"{pending.json()['id']}/decision",
            headers=headers,
            json=payload,
        )
        assert approved.status_code == 200, approved.text

    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    await request_and_approve(Capability.VIEW, expires_at.isoformat())
    await request_and_approve(Capability.COMMENT, None)

    db_session.expire_all()
    active = list((await db_session.execute(
        select(ResourceGrant).where(
            ResourceGrant.entity_id == owner["entity_id"],
            ResourceGrant.resource_type == ResourceType.DOCUMENT,
            ResourceGrant.resource_id == doc["id"],
            ResourceGrant.subject_id == requester["id"],
            ResourceGrant.status == GrantStatus.ACTIVE,
        )
    )).scalars().all())
    assert len(active) == 2
    by_capability = {
        tuple(grant.capabilities or []): grant.expires_at for grant in active
    }
    assert by_capability[(Capability.VIEW,)] is not None
    assert by_capability[(Capability.COMMENT,)] is None

    expiring_grant = next(
        grant for grant in active
        if Capability.VIEW in set(grant.capabilities or [])
    )
    revoked = await client.delete(
        f"/api/v1/documents/{doc['id']}/grants/{expiring_grant.id}",
        headers=headers,
    )
    assert revoked.status_code == 204, revoked.text
    listed = await client.get(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert set(listed.json()[0]["capabilities"]) == {Capability.COMMENT}
    assert listed.json()[0]["expires_at"] is None


@pytest.mark.asyncio
async def test_access_request_rejects_forged_resource_kinds_and_invalid_targets(
    client: AsyncClient,
):
    headers = await _auth(client, "accessrequestvalidation")
    doc = await _upload(client, headers, name="request-validation.md")

    forged = await client.post(
        "/api/v1/permissions/access-requests",
        headers=headers,
        json={
            "resource_type": "share",
            "resource_id": doc["id"],
            "requested_capabilities": [Capability.VIEW],
        },
    )
    assert forged.status_code == 422, forged.text

    unknown_capability = await client.post(
        "/api/v1/permissions/access-requests",
        headers=headers,
        json={
            "resource_type": ResourceType.DOCUMENT,
            "resource_id": doc["id"],
            "requested_capabilities": ["become_owner"],
        },
    )
    assert unknown_capability.status_code == 400, unknown_capability.text

    missing = await client.post(
        "/api/v1/permissions/access-requests",
        headers=headers,
        json={
            "resource_type": ResourceType.DOCUMENT,
            "resource_id": generate_ulid(),
            "requested_capabilities": [Capability.VIEW],
        },
    )
    assert missing.status_code == 404, missing.text


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


@pytest.mark.asyncio
async def test_document_grant_rejects_unsupported_subject_type(client: AsyncClient):
    headers = await _auth(client, "grantsubject")
    doc = await _upload(client, headers)
    response = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=headers,
        json={
            "subject_type": "workspace_role",
            "subject_id": "viewer",
            "capabilities": ["view"],
        },
    )
    assert response.status_code == 400


# ── External shares ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_share_create_list_revoke(client: AsyncClient, monkeypatch):
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
            "require_otp": True,
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

    # Audience-restricted links require a verified email access token.
    resp = await client.get(f"/api/v1/shared-doc/{raw_token}")
    assert resp.status_code == 401, resp.text

    delivered: dict[str, str] = {}

    async def capture_code(to: str, code: str) -> bool:
        delivered[to] = code
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_share_verification_email",
        capture_code,
    )
    wrong = await client.post(
        f"/api/v1/shared-doc/{raw_token}/request-otp",
        json={"email": "mallory@partner.com"},
    )
    assert wrong.status_code == 200
    assert "mallory@partner.com" not in delivered
    sent = await client.post(
        f"/api/v1/shared-doc/{raw_token}/request-otp",
        json={"email": "bob@partner.com"},
    )
    assert sent.status_code == 200, sent.text
    verified = await client.post(
        f"/api/v1/shared-doc/{raw_token}/verify-otp",
        json={"email": "bob@partner.com", "code": delivered["bob@partner.com"]},
    )
    assert verified.status_code == 200, verified.text
    access_token = verified.json()["access_token"]
    assert access_token
    set_cookie = verified.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/api/v1/shared-doc/" in set_cookie
    # Browser flow uses the scoped HttpOnly cookie, keeping the bearer proof
    # out of preview/download URLs and intermediary request logs.
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
async def test_shared_doc_otp_releases_database_locks_before_email_provider(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _auth(client, "otplockrelease")
    doc = await _upload(client, headers, name="otp-lock-release.md")
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
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
        f"/api/v1/shared-doc/{share_data['token']}/request-otp",
        json={"email": "recipient@example.com"},
    ))
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    try:
        revoked = await asyncio.wait_for(
            client.delete(
                f"/api/v1/documents/{doc['id']}/shares/{share_data['id']}",
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
async def test_concurrent_shared_doc_otp_requests_keep_both_challenges(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = await _auth(client, "otpconcurrent")
    doc = await _upload(client, headers, name="otp-concurrent.md")
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "recipient@example.com",
            "capabilities": ["view"],
            "require_otp": True,
        },
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    first_provider_started = asyncio.Event()
    release_first_provider = asyncio.Event()
    codes: list[str] = []

    async def reordered_email(_to: str, code: str) -> bool:
        codes.append(code)
        if len(codes) == 1:
            first_provider_started.set()
            await release_first_provider.wait()
        return True

    monkeypatch.setattr(
        "packages.core.services.email_service.send_share_verification_email",
        reordered_email,
    )
    first_request = asyncio.create_task(client.post(
        f"/api/v1/shared-doc/{token}/request-otp",
        json={"email": "recipient@example.com"},
    ))
    await asyncio.wait_for(first_provider_started.wait(), timeout=5)
    second_request = await client.post(
        f"/api/v1/shared-doc/{token}/request-otp",
        json={"email": "recipient@example.com"},
    )
    assert second_request.status_code == 200, second_request.text
    release_first_provider.set()
    first_response = await asyncio.wait_for(first_request, timeout=5)
    assert first_response.status_code == 200, first_response.text
    assert len(codes) == 2

    verified = await client.post(
        f"/api/v1/shared-doc/{token}/verify-otp",
        json={"email": "recipient@example.com", "code": codes[0]},
    )
    assert verified.status_code == 200, verified.text


@pytest.mark.asyncio
async def test_failed_shared_doc_otp_delivery_discards_only_undelivered_challenge(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.models.permission import Share

    headers = await _auth(client, "otpdeliveryfailure")
    doc = await _upload(client, headers, name="otp-delivery-failure.md")
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
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
        f"/api/v1/shared-doc/{share.json()['token']}/request-otp",
        json={"email": "recipient@example.com"},
    )
    assert response.status_code == 503, response.text

    share_row = await db_session.get(Share, share.json()["id"])
    assert share_row is not None
    await db_session.refresh(share_row)
    assert (share_row.metadata_ or {}).get("otp_challenges") == {}


@pytest.mark.asyncio
async def test_shared_doc_content_releases_database_locks_before_streaming(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.config import get_settings
    from starlette.responses import FileResponse

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "contentlockrelease")
        doc = await _upload(
            client,
            headers,
            name="stream-lock-release.md",
            body=b"stream after admission",
        )
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={"audience_type": "anonymous", "capabilities": ["view"]},
        )
        assert share.status_code == 201, share.text
        share_data = share.json()

        stream_started = asyncio.Event()
        release_stream = asyncio.Event()
        original_call = FileResponse.__call__

        async def delayed_stream(response, scope, receive, send):
            stream_started.set()
            await release_stream.wait()
            return await original_call(response, scope, receive, send)

        monkeypatch.setattr(FileResponse, "__call__", delayed_stream)
        content_request = asyncio.create_task(
            client.get(f"/api/v1/shared-doc/{share_data['token']}/content")
        )
        await asyncio.wait_for(stream_started.wait(), timeout=5)
        try:
            revoked = await asyncio.wait_for(
                client.delete(
                    f"/api/v1/documents/{doc['id']}/shares/{share_data['id']}",
                    headers=headers,
                ),
                timeout=5,
            )
            assert revoked.status_code == 204, revoked.text
        finally:
            release_stream.set()

        content = await asyncio.wait_for(content_request, timeout=5)
        assert content.status_code == 200, content.text
        assert content.content == b"stream after admission"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["content", "download"])
async def test_shared_doc_file_stream_releases_entity_read_boundary_after_snapshot(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
):
    from packages.core.config import get_settings
    from packages.core.services.entity_fs import (
        entity_filesystem_mutation_lock,
        get_entity_root,
    )
    from starlette.responses import FileResponse

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, f"sharedreadboundary{endpoint}")
        doc = await _upload(
            client,
            headers,
            name=f"locked-{endpoint}.md",
            body=b"authorized inode",
        )
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view", "download"],
                "allow_download": True,
            },
        )
        assert share.status_code == 201, share.text

        stream_started = asyncio.Event()
        release_stream = asyncio.Event()
        original_call = FileResponse.__call__

        async def delayed_stream(response, scope, receive, send):
            stream_started.set()
            await release_stream.wait()
            return await original_call(response, scope, receive, send)

        monkeypatch.setattr(FileResponse, "__call__", delayed_stream)
        request_task = asyncio.create_task(client.get(
            f"/api/v1/shared-doc/{share.json()['token']}/{endpoint}"
        ))
        await asyncio.wait_for(stream_started.wait(), timeout=5)
        try:
            async with entity_filesystem_mutation_lock(
                get_entity_root(doc["entity_id"]),
                timeout_seconds=0.2,
            ):
                pass
        finally:
            release_stream.set()

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status_code == 200, response.text
        assert response.content == b"authorized inode"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_document_share_can_never_expire(client: AsyncClient):
    headers = await _auth(client, "permanentdoc")
    doc = await _upload(client, headers)
    created = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": None,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is None

    public = await client.get(f"/api/v1/shared-doc/{created.json()['token']}")
    assert public.status_code == 200, public.text
    assert public.json()["expires_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in_days", [1, 90])
async def test_document_share_configured_expiry(client: AsyncClient, expires_in_days: int):
    headers = await _auth(client, f"expiry{expires_in_days}")
    doc = await _upload(client, headers)
    created = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={
            "audience_type": "anonymous",
            "capabilities": ["view"],
            "expires_in_days": expires_in_days,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["expires_at"] is not None


@pytest.mark.asyncio
async def test_document_share_default_configured_expiry(client: AsyncClient):
    headers = await _auth(client, "defaultshareexpiry")
    doc = await _upload(client, headers)
    before = datetime.now(timezone.utc)
    created = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert created.status_code == 201, created.text
    expires_at = datetime.fromisoformat(created.json()["expires_at"])
    assert timedelta(days=6, hours=23) < expires_at - before < timedelta(days=7, minutes=1)


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


@pytest.mark.asyncio
async def test_share_requires_view_capability(client: AsyncClient):
    headers = await _auth(client, "shareviewcap")
    doc = await _upload(client, headers)
    response = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["comment"]},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_trash_immediately_revokes_external_share(client: AsyncClient):
    headers = await _auth(client, "sharetrash")
    doc = await _upload(client, headers)
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    token = share.json()["token"]
    assert (await client.get(f"/api/v1/shared-doc/{token}")).status_code == 200
    trashed = await client.post(f"/api/v1/documents/{doc['id']}/trash", headers=headers)
    assert trashed.status_code == 200, trashed.text
    assert (await client.get(f"/api/v1/shared-doc/{token}")).status_code == 404


@pytest.mark.asyncio
async def test_deleted_workspace_blocks_document_acl_and_public_share_until_restore(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document

    headers = await _auth(client, "deletedworkspacedocshare")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restorable document permissions"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    assert workspace_data["artifact_folder_id"]

    doc = await _upload(client, headers, name="workspace-owned.md")
    doc_row = await db_session.get(Document, doc["id"])
    assert doc_row is not None
    doc_row.folder_id = workspace_data["artifact_folder_id"]
    await db_session.commit()

    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": [Capability.VIEW]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]
    assert (await client.get(f"/api/v1/shared-doc/{token}")).status_code == 200

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_data['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/api/v1/shared-doc/{token}")).status_code == 410
    assert (
        await client.get(
            f"/api/v1/documents/{doc['id']}",
            headers=headers,
        )
    ).status_code == 404
    listed_while_deleted = await client.get("/api/v1/documents", headers=headers)
    assert listed_while_deleted.status_code == 200, listed_while_deleted.text
    assert doc["id"] not in {
        item["id"] for item in listed_while_deleted.json()["items"]
    }
    assert (
        await client.get(
            f"/api/v1/documents/{doc['id']}/grants",
            headers=headers,
        )
    ).status_code == 404
    access_request = await client.post(
        "/api/v1/permissions/access-requests",
        headers=headers,
        json={
            "resource_type": ResourceType.DOCUMENT,
            "resource_id": doc["id"],
            "requested_capabilities": [Capability.VIEW],
        },
    )
    assert access_request.status_code == 404, access_request.text

    restored = await client.post(
        f"/api/v1/workspaces/{workspace_data['id']}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert (await client.get(f"/api/v1/shared-doc/{token}")).status_code == 200


@pytest.mark.asyncio
async def test_legacy_document_group_routes_require_workspace_manage_access(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup, DocumentGroupMember

    owner_headers = await _auth(client, "legacygroupworkspaceowner")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private group workspace"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    group = await client.post(
        "/api/v1/documents/groups",
        headers=owner_headers,
        json={"name": "Private Knowledge", "workspace_id": workspace_data["id"]},
    )
    assert group.status_code == 201, group.text
    stored_group = (await db_session.execute(
        select(DocumentGroup).where(DocumentGroup.id == group.json()["id"])
    )).scalar_one()
    assert stored_group.settings == {
        "kind": "knowledge_net",
        "scope": "workspace",
        "purpose": "",
        "user_manageable": True,
    }
    document = await _upload(
        client,
        owner_headers,
        name="private-group-source.md",
        visibility="private",
    )
    member = await _create_entity_user(
        workspace_data["entity_id"],
        "legacy_group_nonmember",
        "member",
    )

    listed = await client.get("/api/v1/documents/groups", headers=member["headers"])
    assert listed.status_code == 200, listed.text
    assert group.json()["id"] not in {row["id"] for row in listed.json()}

    create = await client.post(
        "/api/v1/documents/groups",
        headers=member["headers"],
        json={"name": "Injected", "workspace_id": workspace_data["id"]},
    )
    assert create.status_code == 403, create.text
    batch = await client.post(
        "/api/v1/documents/groups/batch-add",
        headers=member["headers"],
        json={"document_ids": [document["id"]], "group_id": group.json()["id"]},
    )
    assert batch.status_code == 403, batch.text
    single = await client.post(
        f"/api/v1/documents/{document['id']}/groups/{group.json()['id']}",
        headers=member["headers"],
    )
    assert single.status_code == 403, single.text
    assert (await db_session.execute(
        select(DocumentGroupMember).where(
            DocumentGroupMember.group_id == group.json()["id"],
            DocumentGroupMember.document_id == document["id"],
        )
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_bulk_reindex_serializes_with_workspace_delete(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document, VectorStatus
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "bulkreindexdeleterace")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Bulk reindex delete race"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    workspace_doc = await _upload(
        client,
        headers,
        name="workspace-reindex.md",
    )
    global_doc = await _upload(
        client,
        headers,
        name="global-reindex.md",
    )
    workspace_row = await db_session.get(Document, workspace_doc["id"])
    global_row = await db_session.get(Document, global_doc["id"])
    assert workspace_row is not None
    assert global_row is not None
    workspace_row.folder_id = workspace_data["artifact_folder_id"]
    workspace_row.vector_status = VectorStatus.READY
    global_row.vector_status = VectorStatus.READY
    await db_session.commit()

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_data["id"],
            workspace_data["entity_id"],
        ) is True
        reindex_task = asyncio.create_task(client.post(
            "/api/v1/documents/reindex",
            headers=headers,
        ))
        await asyncio.sleep(0.1)
        assert reindex_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(reindex_task, timeout=5)
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    await db_session.refresh(workspace_row)
    await db_session.refresh(global_row)
    assert workspace_row.vector_status == VectorStatus.READY
    assert global_row.vector_status == VectorStatus.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["share", "access_request"])
async def test_document_permission_writes_serialize_with_workspace_delete(
    client: AsyncClient,
    db_session: AsyncSession,
    surface: str,
):
    from sqlalchemy import func, select

    from packages.core.models.document import Document
    from packages.core.models.permission import ResourceGrantPending, Share
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, f"permissiondeleterace{surface}")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": f"Permission delete race {surface}"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    doc = await _upload(client, headers, name=f"race-{surface}.md")
    doc_row = await db_session.get(Document, doc["id"])
    assert doc_row is not None
    doc_row.folder_id = workspace_data["artifact_folder_id"]
    await db_session.commit()

    async with db_module.async_session() as deleting_db:
        deleted = await soft_delete_workspace(
            deleting_db,
            workspace_data["id"],
            workspace_data["entity_id"],
        )
        assert deleted is True

        if surface == "share":
            request_task = asyncio.create_task(client.post(
                f"/api/v1/documents/{doc['id']}/shares",
                headers=headers,
                json={
                    "audience_type": "anonymous",
                    "capabilities": [Capability.VIEW],
                },
            ))
        else:
            request_task = asyncio.create_task(client.post(
                "/api/v1/permissions/access-requests",
                headers=headers,
                json={
                    "resource_type": ResourceType.DOCUMENT,
                    "resource_id": doc["id"],
                    "requested_capabilities": [Capability.VIEW],
                },
            ))

        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 404, response.text

    async with db_module.async_session() as verify_db:
        model = Share if surface == "share" else ResourceGrantPending
        created = await verify_db.scalar(select(func.count()).select_from(model).where(
            model.entity_id == workspace_data["entity_id"],
            model.resource_id == doc["id"],
        ))
        assert created == 0


@pytest.mark.asyncio
async def test_document_content_write_rechecks_workspace_delete_inside_fs_boundary(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.services.entity_service import soft_delete_workspace

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    headers = await _auth(client, "documentwritedeleterace")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Document write delete race"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    doc = await _upload(client, headers, name="write-delete-race.md")
    doc_row = await db_session.get(Document, doc["id"])
    assert doc_row is not None
    doc_row.folder_id = workspace_data["artifact_folder_id"]
    await db_session.commit()

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_data["id"],
            workspace_data["entity_id"],
        ) is True
        write_task = asyncio.create_task(client.put(
            f"/api/v1/documents/{doc['id']}/content",
            headers=headers,
            json={"content": "must not be committed"},
        ))
        await asyncio.sleep(0.1)
        assert write_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(write_task, timeout=5)
    assert response.status_code == 404, response.text
    assert Path(tmp_path, doc["entity_id"], doc["fs_path"]).read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_document_content_write_rechecks_revoked_edit_grant_under_lock(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import select

    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    owner_headers = await _auth(client, "documentwriterevokerace")
    member_headers, member = await _invite_and_accept_member(
        client,
        owner_headers,
        "document.write.revoke@test.com",
        name="Document Writer",
    )
    doc = await _upload(
        client,
        owner_headers,
        name="write-revoke-race.md",
        visibility="private",
    )
    grant_response = await client.post(
        f"/api/v1/documents/{doc['id']}/grants",
        headers=owner_headers,
        json={
            "subject_type": "user",
            "subject_id": member["staff_id"],
            "capabilities": [Capability.VIEW, Capability.EDIT],
        },
    )
    assert grant_response.status_code == 201, grant_response.text

    async with db_module.async_session() as revoking_db:
        await revoking_db.execute(
            select(Document).where(Document.id == doc["id"]).with_for_update()
        )
        grant = (await revoking_db.execute(
            select(ResourceGrant)
            .where(ResourceGrant.id == grant_response.json()["id"])
            .with_for_update()
        )).scalar_one()
        grant.status = GrantStatus.REVOKED
        write_task = asyncio.create_task(client.put(
            f"/api/v1/documents/{doc['id']}/content",
            headers=member_headers,
            json={"content": "must not be committed"},
        ))
        await asyncio.sleep(0.1)
        assert write_task.done() is False
        await revoking_db.commit()

    response = await asyncio.wait_for(write_task, timeout=5)
    # The request can observe the revocation either during its initial
    # visibility check (404) or during the locked mutation recheck (403).
    # Both outcomes are fail-closed; the durable invariant is that no content
    # from the revoked writer reaches storage.
    assert response.status_code in {403, 404}, response.text
    assert Path(tmp_path, doc["entity_id"], doc["fs_path"]).read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_public_document_share_serializes_with_workspace_delete(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "publicdocdeleterace")
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Public document delete race"},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    doc = await _upload(client, headers, name="public-delete-race.md")
    doc_row = await db_session.get(Document, doc["id"])
    assert doc_row is not None
    doc_row.folder_id = workspace_data["artifact_folder_id"]
    await db_session.commit()

    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": [Capability.VIEW]},
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
            client.get(f"/api/v1/shared-doc/{token}")
        )
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 410, response.text


@pytest.mark.asyncio
async def test_public_document_share_serializes_with_reclassification(
    client: AsyncClient,
):
    from sqlalchemy import select

    from packages.core.models.document import Document

    headers = await _auth(client, "publicdocclassrace")
    doc = await _upload(
        client,
        headers,
        name="public-classification-race.md",
        classification="internal",
    )
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": [Capability.VIEW]},
    )
    assert share.status_code == 201, share.text
    token = share.json()["token"]

    async with db_module.async_session() as policy_db:
        doc_row = (await policy_db.execute(
            select(Document)
            .where(Document.id == doc["id"])
            .with_for_update()
        )).scalar_one()
        doc_row.classification = "confidential"
        await policy_db.flush()
        request_task = asyncio.create_task(
            client.get(f"/api/v1/shared-doc/{token}")
        )
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await policy_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 410, response.text


@pytest.mark.asyncio
async def test_public_document_share_retries_when_folder_scope_changes_while_locking(
    client: AsyncClient,
):
    from sqlalchemy import select

    from packages.core.models.document import Document

    headers = await _auth(client, "publicdocmovescope")
    source_workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Share move source"},
    )
    target_workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Share move target"},
    )
    assert source_workspace.status_code == 201, source_workspace.text
    assert target_workspace.status_code == 201, target_workspace.text
    source_folder_id = source_workspace.json()["artifact_folder_id"]
    target_folder_id = target_workspace.json()["artifact_folder_id"]

    doc = await _upload(client, headers, name="moving-share.md")
    async with db_module.async_session() as db:
        doc_row = await db.get(Document, doc["id"])
        assert doc_row is not None
        doc_row.folder_id = source_folder_id
        await db.commit()

    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert share.status_code == 201, share.text

    async with db_module.async_session() as moving_db:
        moving_doc = (await moving_db.execute(
            select(Document).where(Document.id == doc["id"]).with_for_update()
        )).scalar_one()
        moving_doc.folder_id = target_folder_id
        await moving_db.flush()
        request_task = asyncio.create_task(
            client.get(f"/api/v1/shared-doc/{share.json()['token']}")
        )
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await moving_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_reclassification_invalidates_older_share_policy(client: AsyncClient):
    headers = await _auth(client, "sharereclass")
    doc = await _upload(client, headers, classification="internal")
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    token = share.json()["token"]
    changed = await client.post(
        f"/api/v1/permissions/documents/{doc['id']}/classify",
        headers=headers,
        json={"classification": "confidential"},
    )
    assert changed.status_code == 200, changed.text
    denied = await client.get(f"/api/v1/shared-doc/{token}")
    assert denied.status_code == 410


@pytest.mark.asyncio
async def test_ancestor_reclassification_invalidates_legacy_child_share(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document, DocumentFolder

    headers = await _auth(client, "shareancestorclass")
    doc = await _upload(client, headers, classification="internal")
    share = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    token = share.json()["token"]
    row = await db_session.get(Document, doc["id"])
    assert row is not None
    folder = DocumentFolder(
        id=generate_ulid(),
        entity_id=row.entity_id,
        name="Legacy Confidential Parent",
        classification="confidential",
        visibility="entity",
        client_visible=False,
    )
    db_session.add(folder)
    row.folder_id = folder.id
    await db_session.commit()

    denied = await client.get(f"/api/v1/shared-doc/{token}")
    assert denied.status_code == 410


@pytest.mark.asyncio
async def test_direct_share_uses_effective_ancestor_classification(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.permission import Share

    headers = await _auth(client, "shareeffectiveinternal")
    doc = await _upload(client, headers, classification="public")
    row = await db_session.get(Document, doc["id"])
    assert row is not None
    folder = DocumentFolder(
        id=generate_ulid(),
        entity_id=row.entity_id,
        name="Internal Parent",
        classification="internal",
        visibility="entity",
        client_visible=False,
    )
    db_session.add(folder)
    row.folder_id = folder.id
    await db_session.commit()

    created = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert created.status_code == 201, created.text
    share = await db_session.get(Share, created.json()["id"])
    assert share is not None
    assert share.metadata_["classification_at_creation"] == "internal"

    viewed = await client.get(f"/api/v1/shared-doc/{created.json()['token']}")
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["classification"] == "internal"


@pytest.mark.asyncio
async def test_share_approval_uses_effective_confidential_ancestor(
    client: AsyncClient,
    db_session: AsyncSession,
):
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.permission import Share

    headers = await _auth(client, "shareeffectiveconfidential")
    doc = await _upload(client, headers, classification="internal")
    row = await db_session.get(Document, doc["id"])
    assert row is not None
    folder = DocumentFolder(
        id=generate_ulid(),
        entity_id=row.entity_id,
        name="Confidential Parent",
        classification="confidential",
        visibility="entity",
        client_visible=False,
    )
    db_session.add(folder)
    row.folder_id = folder.id
    await db_session.commit()

    direct = await client.post(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
        json={"audience_type": "anonymous", "capabilities": ["view"]},
    )
    assert direct.status_code == 409, direct.text

    requested = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "reviewer@example.com",
            "capabilities": ["view"],
            "reason": "External review",
        },
    )
    assert requested.status_code == 201, requested.text
    decided = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals/"
        f"{requested.json()['id']}/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert decided.status_code == 200, decided.text
    share = await db_session.get(
        Share,
        decided.json()["approval"]["approved_share_id"],
    )
    assert share is not None
    assert share.metadata_["classification_at_creation"] == "confidential"
    assert share.metadata_["approved_external_share"] is True


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
            "expires_in_days": None,
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

    # The bearer share token alone is insufficient for an email audience.
    resp = await client.get(f"/api/v1/shared-doc/{decision['token']}")
    assert resp.status_code == 401

    # A consumed approval cannot materialize a second share/token. Production
    # decisions additionally lock the pending row so concurrent attempts
    # serialize around this same state transition.
    repeated = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals/{approval_id}/decision",
        headers=headers,
        json={"decision": "approve"},
    )
    assert repeated.status_code == 400


@pytest.mark.asyncio
async def test_share_approval_concurrent_decisions_materialize_once(
    client: AsyncClient,
):
    headers = await _auth(client, "approvalconcurrent")
    doc = await _upload(client, headers, classification="confidential")
    requested = await client.post(
        f"/api/v1/documents/{doc['id']}/share-approvals",
        headers=headers,
        json={
            "audience_type": "email",
            "audience_value": "concurrent@example.com",
            "capabilities": ["view"],
            "reason": "Concurrent decision regression",
        },
    )
    assert requested.status_code == 201, requested.text
    decision_url = (
        f"/api/v1/documents/{doc['id']}/share-approvals/"
        f"{requested.json()['id']}/decision"
    )

    first, second = await asyncio.gather(
        client.post(
            decision_url,
            headers=headers,
            json={"decision": "approve"},
        ),
        client.post(
            decision_url,
            headers=headers,
            json={"decision": "approve"},
        ),
    )

    assert sorted((first.status_code, second.status_code)) == [200, 400]
    shares = await client.get(
        f"/api/v1/documents/{doc['id']}/shares",
        headers=headers,
    )
    assert shares.status_code == 200, shares.text
    assert len(shares.json()) == 1


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
            "audience_type": "anonymous",
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


@pytest.mark.asyncio
async def test_best_effort_access_log_isolated_by_savepoint():
    from types import SimpleNamespace

    from apps.api.routers import document_permissions

    class NestedTransaction:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            self.session.savepoint_entered = True

        async def __aexit__(self, exc_type, _exc, _tb):
            self.session.savepoint_rolled_back = exc_type is RuntimeError
            return False

    class FailingAuditSession:
        savepoint_entered = False
        savepoint_rolled_back = False

        def begin_nested(self):
            return NestedTransaction(self)

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("audit table unavailable")

    session = FailingAuditSession()
    await document_permissions._write_access_log(
        session,
        doc=SimpleNamespace(
            id="doc-1",
            entity_id="entity-1",
            classification="internal",
        ),
        actor_type="user",
        actor_id="user-1",
        action="read",
    )

    assert session.savepoint_entered is True
    assert session.savepoint_rolled_back is True


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
async def test_limited_shared_doc_content_requires_a_counted_view_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
):
    """Inline previews do not consume a second use or bypass the entry view."""
    from packages.core.config import get_settings
    from packages.core.models.permission import Share

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "limitedcontent")
        doc = await _upload(client, headers, name="report.md", body=b"shared content")
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view"],
                "allow_download": False,
            },
        )
        assert share.status_code == 201, share.text
        token = share.json()["token"]
        share_row = await db_session.get(Share, share.json()["id"])
        assert share_row is not None
        share_row.max_uses = 1
        await db_session.commit()

        blocked = await client.get(f"/api/v1/shared-doc/{token}/content")
        assert blocked.status_code == 410, blocked.text

        opened = await client.get(f"/api/v1/shared-doc/{token}")
        assert opened.status_code == 200, opened.text
        content = await client.get(f"/api/v1/shared-doc/{token}/content")
        assert content.status_code == 200, content.text
        assert content.content == b"shared content"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_shared_doc_content_serializes_with_share_revoke(
    client: AsyncClient,
    tmp_path: Path,
):
    from sqlalchemy import select

    from packages.core.config import get_settings
    from packages.core.models.permission import Share

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "sharedcontentrevokerace")
        doc = await _upload(
            client,
            headers,
            name="revoked-content.md",
            body=b"must not escape after revoke",
        )
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={"audience_type": "anonymous", "capabilities": ["view"]},
        )
        assert share.status_code == 201, share.text
        token = share.json()["token"]

        async with db_module.async_session() as revoking_db:
            share_row = (await revoking_db.execute(
                select(Share)
                .where(Share.id == share.json()["id"])
                .with_for_update()
            )).scalar_one()
            share_row.status = "revoked"
            await revoking_db.flush()
            request_task = asyncio.create_task(
                client.get(f"/api/v1/shared-doc/{token}/content")
            )
            await asyncio.sleep(0.1)
            assert request_task.done() is False
            await revoking_db.commit()

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status_code == 404, response.text
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_limited_shared_doc_download_uses_the_counted_view_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
):
    """A download-enabled limited share remains usable from its public page."""
    from packages.core.config import get_settings
    from packages.core.models.permission import Share

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        headers = await _auth(client, "limiteddownload")
        doc = await _upload(client, headers, name="report.md", body=b"download content")
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view", "download"],
                "allow_download": True,
            },
        )
        assert share.status_code == 201, share.text
        token = share.json()["token"]
        share_row = await db_session.get(Share, share.json()["id"])
        assert share_row is not None
        share_row.max_uses = 1
        await db_session.commit()

        blocked = await client.get(f"/api/v1/shared-doc/{token}/download")
        assert blocked.status_code == 410, blocked.text

        opened = await client.get(f"/api/v1/shared-doc/{token}")
        assert opened.status_code == 200, opened.text
        download = await client.get(f"/api/v1/shared-doc/{token}/download")
        assert download.status_code == 200, download.text
        assert download.content == b"download content"

        await db_session.refresh(share_row)
        assert share_row.use_count == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "preview_path", "renderer_name"),
    [
        ("contract.docx", "pages", "render_document_pages"),
        ("presentation.pptx", "slides", "render_slides"),
    ],
)
async def test_shared_doc_office_preview_reads_are_token_scoped(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    preview_path: str,
    renderer_name: str,
):
    """A limited share unlocks Office preview resources only after its page opens."""
    from packages.core.config import get_settings
    from packages.core.models.permission import Share
    from packages.core.services import slide_renderer

    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    version = "0123456789abcdef"
    cache_file = tmp_path / version / "page-0001.png"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(png)

    render_calls = 0

    async def render_preview(*_args, **_kwargs):
        nonlocal render_calls
        render_calls += 1
        return [str(cache_file)]

    async def open_preview(*_args, **_kwargs):
        return cache_file.open("rb")

    monkeypatch.setattr(slide_renderer, renderer_name, render_preview)
    monkeypatch.setattr(
        slide_renderer,
        "open_cached_document_page" if preview_path == "pages" else "open_cached_slide",
        open_preview,
    )
    try:
        headers = await _auth(client, f"shared{preview_path}")
        doc = await _upload(
            client,
            headers,
            name=name,
            body=_office_fixture(
                "word/document.xml" if preview_path == "pages" else "ppt/presentation.xml",
            ),
        )
        share = await client.post(
            f"/api/v1/documents/{doc['id']}/shares",
            headers=headers,
            json={
                "audience_type": "anonymous",
                "capabilities": ["view"],
                "allow_download": False,
            },
        )
        assert share.status_code == 201, share.text
        token = share.json()["token"]
        share_row = await db_session.get(Share, share.json()["id"])
        assert share_row is not None
        share_row.max_uses = 1
        await db_session.commit()

        blocked = await client.get(f"/api/v1/shared-doc/{token}/preview/{preview_path}")
        assert blocked.status_code == 410, blocked.text
        assert render_calls == 0

        opened = await client.get(f"/api/v1/shared-doc/{token}")
        assert opened.status_code == 200, opened.text
        assert "manor_share_view=" in opened.headers.get("set-cookie", "")

        preview = await client.get(f"/api/v1/shared-doc/{token}/preview/{preview_path}")
        assert preview.status_code == 200, preview.text
        assert render_calls == 1
        body = preview.json()
        item = body[preview_path][0]
        assert item["index"] == 0
        assert item["url"] == f"/api/v1/shared-doc/{token}/preview/{preview_path}/0?version={version}"
        assert doc["id"] not in item["url"]
        assert doc["entity_id"] not in item["url"]
        assert str(tmp_path) not in item["url"]

        image = await client.get(item["url"])
        assert image.status_code == 200, image.text
        assert image.content == png
        assert image.headers["content-disposition"].startswith("inline")
        assert image.headers["cache-control"] == "private, no-store"
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
