"""E2E tests for authorize()'s folder grant walk-up (Phase B follow-up).

Setup: registers a fresh user (entity owner by default), enables the
``permissions_v1_enforce`` feature flag for that entity, then verifies
that a grant on an *ancestor folder* lets a second user read a child
document — even though that user has no doc-level grant.

The flag gate is important: with the flag OFF, ``authorize()`` falls
through to the legacy verb check on tenant role and these tests would
trivially pass without exercising the walk-up logic.
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.base import generate_ulid
from packages.core.models.user import User
from packages.core.services.auth_service import hash_password


# ── Helpers ──────────────────────────────────────────────────────────────


async def _auth(client: AsyncClient, username: str) -> tuple[dict, str]:
    """Register; return (headers, user_id)."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@walkup.test",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body["user_id"]


async def _enable_walkup_flag(db_session: AsyncSession) -> None:
    """Globally enable ``permissions_v1_enforce`` so the strict branch runs."""
    row = (
        await db_session.execute(select(FeatureFlag).where(FeatureFlag.key == "permissions_v1_enforce"))
    ).scalar_one_or_none()
    if row:
        row.default_enabled = True
    else:
        db_session.add(
            FeatureFlag(
                key="permissions_v1_enforce",
                description="enables strict permission-v1 branch in authorize()",
                default_enabled=True,
                status="active",
            )
        )
    await db_session.commit()


async def _create_entity_user(
    db_session: AsyncSession,
    *,
    entity_id: str,
    username: str,
    role: str = "viewer",
) -> str:
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"{username}@walkup.test",
        display_name=username.replace("_", " ").title(),
        password_hash=hash_password("pass123"),
        role=role,
        status="active",
    )
    db_session.add(user)
    await db_session.commit()
    return user.id


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
    name: str = "doc.md",
    visibility: str = "private",
) -> dict:
    """Upload a doc — defaults to ``visibility=private`` so the
    ``visibility.entity`` short-circuit in authorize() does not preempt
    layer-3 grant lookups. Tests focused on grant logic need this."""
    params: list[tuple[str, str]] = [("visibility", visibility)]
    if folder_id:
        params.append(("folder_id", folder_id))
    url = f"/api/v1/documents/upload?{urlencode(params)}"
    resp = await client.post(
        url,
        headers=headers,
        files={"file": (name, b"hi", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walkup_direct_folder_grant_covers_child_doc(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Grant on the immediate parent folder → user can read the doc."""
    await _enable_walkup_flag(db_session)
    owner_h, _owner_id = await _auth(client, "walkup_owner1")
    folder = await _create_folder(client, owner_h, "Shared Bay")
    doc = await _upload(client, owner_h, folder_id=folder["id"], name="bay.md")

    member_user_id = await _create_entity_user(
        db_session,
        entity_id=doc["entity_id"],
        username="walkup_member1",
    )
    resp = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=owner_h,
        json={
            "subject_type": "user",
            "subject_id": member_user_id,
            "capabilities": ["view"],
        },
    )
    assert resp.status_code == 201, resp.text

    # Now hit authorize() directly — that's the API-private surface, so
    # we exercise it via a thin async call rather than the HTTP layer.
    from packages.core.auth import UserActor, authorize
    from packages.core.models import ResourceType
    from packages.core.auth.authz import Resource

    actor = UserActor(
        user_id=member_user_id,
        entity_id=doc["entity_id"],
        role="viewer",
    )
    decision = await authorize(
        db_session,
        actor,
        "view",
        Resource(
            type=ResourceType.DOCUMENT,
            id=doc["id"],
            entity_id=doc["entity_id"],
            visibility=doc.get("visibility"),
            classification=doc.get("classification"),
            owner_id=doc.get("owner_id"),
        ),
    )
    assert decision.allow, f"Walk-up should allow; got reason={decision.reason}"
    assert decision.matched_rule == "layer3.folder_walkup"


@pytest.mark.asyncio
async def test_walkup_grandparent_folder_grant_covers_child_doc(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Grant on a *grandparent* folder also lets user read the doc."""
    await _enable_walkup_flag(db_session)
    owner_h, _ = await _auth(client, "walkup_owner2")
    grandparent = await _create_folder(client, owner_h, "Org")
    parent = await _create_folder(
        client,
        owner_h,
        "Engineering",
        parent_id=grandparent["id"],
    )
    doc = await _upload(client, owner_h, folder_id=parent["id"], name="deep.md")

    member_user_id = await _create_entity_user(
        db_session,
        entity_id=doc["entity_id"],
        username="walkup_member2",
    )
    resp = await client.post(
        f"/api/v1/folders/{grandparent['id']}/grants",
        headers=owner_h,
        json={
            "subject_type": "user",
            "subject_id": member_user_id,
            "capabilities": ["view", "comment"],
        },
    )
    assert resp.status_code == 201

    from packages.core.auth import UserActor, authorize
    from packages.core.models import ResourceType
    from packages.core.auth.authz import Resource

    actor = UserActor(
        user_id=member_user_id,
        entity_id=doc["entity_id"],
        role="viewer",
    )
    decision = await authorize(
        db_session,
        actor,
        "comment",
        Resource(
            type=ResourceType.DOCUMENT,
            id=doc["id"],
            entity_id=doc["entity_id"],
            visibility=doc.get("visibility"),
            classification=doc.get("classification"),
            owner_id=doc.get("owner_id"),
        ),
    )
    assert decision.allow, f"Grandparent walk-up should allow; reason={decision.reason}"
    assert decision.matched_rule == "layer3.folder_walkup"


@pytest.mark.asyncio
async def test_walkup_revoked_folder_grant_denies(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Revoking the folder grant kills the walk-up immediately."""
    await _enable_walkup_flag(db_session)
    owner_h, _ = await _auth(client, "walkup_owner3")
    folder = await _create_folder(client, owner_h, "Revoke Test")
    doc = await _upload(client, owner_h, folder_id=folder["id"], name="rv.md")

    member_user_id = await _create_entity_user(
        db_session,
        entity_id=doc["entity_id"],
        username="walkup_member3",
    )
    grant_resp = await client.post(
        f"/api/v1/folders/{folder['id']}/grants",
        headers=owner_h,
        json={
            "subject_type": "user",
            "subject_id": member_user_id,
            "capabilities": ["view"],
        },
    )
    grant_id = grant_resp.json()["id"]

    # Revoke
    rev = await client.delete(
        f"/api/v1/folders/{folder['id']}/grants/{grant_id}",
        headers=owner_h,
    )
    assert rev.status_code == 204

    from packages.core.auth import UserActor, authorize
    from packages.core.models import ResourceType
    from packages.core.auth.authz import Resource

    actor = UserActor(
        user_id=member_user_id,
        entity_id=doc["entity_id"],
        role="viewer",
    )
    decision = await authorize(
        db_session,
        actor,
        "view",
        Resource(
            type=ResourceType.DOCUMENT,
            id=doc["id"],
            entity_id=doc["entity_id"],
            visibility=doc.get("visibility"),
            classification=doc.get("classification"),
            owner_id=doc.get("owner_id"),
        ),
    )
    assert not decision.allow
