"""Standing-grants management API (M8).

``GET  /workspaces/{id}/governance/standing-grants``  — any entity member
``DELETE …/standing-grants/{kind}/{value}``           — owner/admin only;
revoking removes the grant from the policy and writes a GovernanceRevision
audit row.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from auth_helpers import register_user_and_get_token
from packages.core.models.base import generate_ulid
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace

ACTION_KEY = "workspace.proposal.task"
# NOT one of DEFAULT_AUTO_APPROVE_CAPABILITIES — the default policy already
# grants file.write/manor.composite, so use a distinct id to observe the
# add → revoke round-trip.
CAPABILITY_ID = "browser.use"


async def _register_owner(client: AsyncClient, username: str) -> tuple[dict, str, str]:
    resp = await register_user_and_get_token(
        client,
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    data = resp.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, data["user_id"], me.json()["entity_id"]


async def _seed_workspace_with_grants(entity_id: str) -> str:
    import packages.core.database as dbmod
    from packages.core.governance.service import (
        add_auto_approve_action,
        add_auto_approve_capability,
    )

    async with dbmod.async_session() as db:
        workspace = Workspace(
            id=generate_ulid(),
            entity_id=entity_id,
            name="Standing Grants WS",
            status="active",
            settings={},
        )
        db.add(workspace)
        await db.commit()
        await add_auto_approve_action(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            action_key=ACTION_KEY,
            changed_by="test",
        )
        await add_auto_approve_capability(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            capability_id=CAPABILITY_ID,
            changed_by="test",
        )
        await db.commit()
    return workspace.id


async def _member_headers(entity_id: str, username: str) -> dict:
    import packages.core.database as dbmod
    from packages.core.services.auth_service import create_access_token, hash_password

    async with dbmod.async_session() as db:
        member = User(
            id=generate_ulid(),
            entity_id=entity_id,
            email=f"{username}@test.com",
            display_name=username,
            password_hash=hash_password("pass123"),
            role="member",
        )
        db.add_all([
            member,
            UserMembership(
                id=generate_ulid(),
                user_id=member.id,
                entity_id=entity_id,
                role="member",
                status="active",
                is_primary=True,
            ),
        ])
        await db.commit()
    token = create_access_token(member.id, entity_id, "member")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_member_can_read_but_not_revoke(client: AsyncClient):
    _, _, entity_id = await _register_owner(client, "grants_member_owner")
    workspace_id = await _seed_workspace_with_grants(entity_id)
    member = await _member_headers(entity_id, "grants_member_user")

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants",
        headers=member,
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["actions"] == [ACTION_KEY]
    # Default capability grants (file.write/manor.composite) ride along.
    assert CAPABILITY_ID in body["capabilities"]

    denied = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants/action/{ACTION_KEY}",
        headers=member,
    )
    assert denied.status_code == 403, denied.text

    # 403 must not have removed anything.
    still = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants",
        headers=member,
    )
    assert still.json()["actions"] == [ACTION_KEY]


@pytest.mark.asyncio
async def test_admin_revoke_removes_grant_and_writes_revision(client: AsyncClient):
    import packages.core.database as dbmod

    owner, _, entity_id = await _register_owner(client, "grants_admin_owner")
    workspace_id = await _seed_workspace_with_grants(entity_id)

    async with dbmod.async_session() as db:
        before = (await db.execute(
            select(GovernanceRevision).where(
                GovernanceRevision.workspace_id == workspace_id
            )
        )).scalars().all()
        revisions_before = len(before)

    removed = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants/action/{ACTION_KEY}",
        headers=owner,
    )
    assert removed.status_code == 200, removed.text
    removed_body = removed.json()
    assert removed_body["actions"] == []
    assert CAPABILITY_ID in removed_body["capabilities"]

    async with dbmod.async_session() as db:
        policy_row = (await db.execute(
            select(GovernancePolicy).where(
                GovernancePolicy.workspace_id == workspace_id
            )
        )).scalar_one()
        assert ACTION_KEY not in policy_row.policy["auto_approve_actions"]

        revisions = list((await db.execute(
            select(GovernanceRevision).where(
                GovernanceRevision.workspace_id == workspace_id
            ).order_by(GovernanceRevision.revision.asc())
        )).scalars().all())
        assert len(revisions) == revisions_before + 1
        assert revisions[-1].change_summary == (
            f"revoke always-approve action: {ACTION_KEY}"
        )

    # Capability grants revoke through the same endpoint with kind=capability.
    cap_removed = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants/capability/{CAPABILITY_ID}",
        headers=owner,
    )
    assert cap_removed.status_code == 200, cap_removed.text
    assert CAPABILITY_ID not in cap_removed.json()["capabilities"]


@pytest.mark.asyncio
async def test_revoke_unknown_grant_and_bad_kind(client: AsyncClient):
    owner, _, entity_id = await _register_owner(client, "grants_edge_owner")
    workspace_id = await _seed_workspace_with_grants(entity_id)

    missing = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants/action/never.granted",
        headers=owner,
    )
    assert missing.status_code == 404, missing.text

    bad_kind = await client.delete(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants/scope/{ACTION_KEY}",
        headers=owner,
    )
    assert bad_kind.status_code == 400, bad_kind.text

    other_entity_owner, _, _ = await _register_owner(client, "grants_stranger")
    cross = await client.get(
        f"/api/v1/workspaces/{workspace_id}/governance/standing-grants",
        headers=other_entity_owner,
    )
    assert cross.status_code == 404, cross.text
